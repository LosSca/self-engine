"""The town planner proposes; the world still decides.

These tests never touch a real model: the one thing worth pinning down is the
boundary, not the poetry. A fake LLM answers whatever the test needs, and the
assertions are about what reached the schedule and what did not.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.engines.game_clock import Clock
from app.models.game import (
    EntityDef,
    GameState,
    LocationDef,
    RoutineEntryDef,
    WorldDefinition,
)
from app.services import town_advisor


class FakeLLM:
    """Answers with a canned string and remembers what it was asked."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def complete(self, messages, **_kwargs):
        self.calls.append(messages)
        return self.answer


def _world() -> WorldDefinition:
    return WorldDefinition(
        world_id="planner_town",
        name="Planner Town",
        start_location_id="square",
        locations=[
            LocationDef(id="square", name="Square"),
            LocationDef(id="mill", name="Mill"),
        ],
        entities=[
            EntityDef(
                id="miller",
                name="Miller",
                location_id="mill",
                schedule=[
                    RoutineEntryDef(start="08:00", activity="work", location_id="mill")
                ],
            )
        ],
    )


def _state(campaign_id="camp-plan") -> GameState:
    return GameState(
        campaign_id=campaign_id,
        world_id="planner_town",
        location_id="square",
        day=1,
        minute=8 * 60,
    )


class FakeStore:
    """Only the schedule surface the planner is allowed to use."""

    def __init__(self, world):
        self.world = world
        self.scheduled = []

    def get_world(self, world_id):
        return self.world if world_id == self.world.world_id else None

    def schedule_event(self, campaign_id, **kwargs):
        self.scheduled.append(dict(kwargs, campaign_id=campaign_id))
        return "sched-1"


class FakeService:
    def __init__(self, store):
        self.store = store


@pytest.fixture(autouse=True)
def _planner_on(monkeypatch):
    monkeypatch.setenv(town_advisor.ENV_FLAG, "1")


def test_the_planner_is_off_unless_an_author_asks(monkeypatch):
    monkeypatch.delenv(town_advisor.ENV_FLAG, raising=False)
    store = FakeStore(_world())
    llm = FakeLLM(json.dumps([{"kind": "npc.routine", "in_minutes": 5, "payload": {}}]))

    assert asyncio.run(town_advisor.advise(FakeService(store), _state(), llm=llm)) == []
    assert llm.calls == []
    assert store.scheduled == []


def test_a_proposal_lands_in_the_schedule_and_nothing_else():
    world, state = _world(), _state()
    store = FakeStore(world)
    answer = json.dumps(
        [
            {
                "kind": "npc.routine",
                "in_minutes": 90,
                "payload": {"npc_id": "miller", "activity": "socialise"},
            }
        ]
    )

    written = asyncio.run(town_advisor.advise(FakeService(store), state, llm=FakeLLM(answer)))

    assert len(written) == 1
    assert len(store.scheduled) == 1
    row = store.scheduled[0]
    # The clock decides when: the proposal is the intent, not the moment.
    assert row["due_abs_minute"] == Clock.from_state(state).abs_minute + 90
    assert row["kind"] == "npc.routine"
    assert row["payload"] == {"npc_id": "miller", "activity": "socialise"}
    # The state the caller handed over is untouched: advising never writes.
    assert state.day == 1 and state.minute == 8 * 60
    assert state.entities == {}
    # Nor does it claim any part of the world: it is a suggestion, not a turn.
    assert not hasattr(store, "commit_action")


def test_a_kind_the_world_cannot_execute_is_dropped():
    raw = json.dumps(
        [
            {"kind": "player.teleport", "in_minutes": 5, "payload": {}},
            {"kind": "npc.routine", "in_minutes": 5, "payload": {"npc_id": "miller"}},
        ]
    )

    proposals = town_advisor.parse_proposals(raw)

    assert [proposal["kind"] for proposal in proposals] == ["npc.routine"]


def test_proposals_beyond_one_game_day_or_an_unknown_npc_never_schedule():
    world, state = _world(), _state()
    store = FakeStore(world)
    proposals = [
        {"kind": "npc.routine", "in_minutes": 30, "payload": {"npc_id": "ghost"}},
        {"kind": "npc.routine", "in_minutes": 5000, "payload": {"npc_id": "miller"}},
    ]

    written = town_advisor.schedule_proposals(
        store, state.campaign_id, state, proposals, world
    )

    assert written == []
    assert store.scheduled == []


def test_a_babbling_model_is_indistinguishable_from_a_quiet_town():
    world, state = _world(), _state()
    store = FakeStore(world)

    for answer in ("", "I think the miller might go home.", "[{", "null"):
        written = asyncio.run(
            town_advisor.advise(FakeService(store), state, llm=FakeLLM(answer))
        )
        assert written == []
    assert store.scheduled == []


def test_without_a_model_nothing_is_asked_and_nothing_is_written():
    store = FakeStore(_world())

    assert asyncio.run(town_advisor.advise(FakeService(store), _state(), llm=None)) == []
    assert store.scheduled == []


def test_fenced_json_is_read_like_plain_json():
    """Models wrap JSON in fences; that is formatting, not a wrong answer."""
    fenced = (
        "\x60\x60\x60json\n"
        + json.dumps(
            [{"kind": "npc.routine", "in_minutes": 10, "payload": {"npc_id": "miller"}}]
        )
        + "\n\x60\x60\x60"
    )

    assert len(town_advisor.parse_proposals(fenced)) == 1
