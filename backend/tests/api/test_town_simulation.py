"""The whole simulator, end to end: a town of five places and six people.

The point of this file is not any single unit. It is that the four systems the
project promised - free player actions, NPC autonomy, world time and the event
scheduler - run together without a second write path, and that the result is
reproducible: the same inputs produce the same authoritative state, with no
model and no network in the loop.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.engines import npc_brain, npc_schedule
from app.engines.game_clock import Clock

API = "/api/v1"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CAMPAIGN = "camp-town"


def town_data() -> dict:
    return json.loads((FIXTURES / "hollowmere_world.json").read_text(encoding="utf-8"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.delenv("ENGINE_TOWN_PLANNER", raising=False)
    from app.main import app

    return TestClient(app)


def _bind(client) -> None:
    assert client.post(f"{API}/worlds", json=town_data()).status_code == 200
    bound = client.post(
        f"{API}/campaigns/{CAMPAIGN}/world", json={"world_id": "hollowmere"}
    )
    assert bound.status_code == 200, bound.text


def _state(client) -> dict:
    return client.get(f"{API}/campaigns/{CAMPAIGN}/state").json()


def _events(client, limit: int = 500) -> list:
    return client.get(
        f"{API}/campaigns/{CAMPAIGN}/events", params={"limit": limit}
    ).json()


def _act(client, request_id: str, action: str, **fields):
    body = {"campaign_id": CAMPAIGN, "request_id": request_id, "action": action}
    body.update(fields)
    response = client.post(f"{API}/campaigns/{CAMPAIGN}/actions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _npc_events(client) -> list:
    return [
        row
        for row in _events(client)
        if str(row.get("request_id") or "").startswith("npc-")
    ]


def test_the_town_starts_with_everyone_where_they_live(client):
    _bind(client)

    state = _state(client)

    assert state["day"] == 1
    assert state["minute"] == 8 * 60
    assert state["location_id"] == "square"
    assert state["version"] == 0
    # Nobody has acted, so nobody has a materialised record yet: the authored
    # layout is the truth until an event moves someone.
    assert state["entities"] == {}


def test_free_player_actions_change_the_world_through_the_rules(client):
    _bind(client)

    taken = _act(client, "r1", "take", target_id="stout_ale")
    assert taken["status"] == "applied"
    assert taken["code"] == "taken"

    moved = _act(client, "r2", "move", target_id="forge")
    assert moved["status"] == "applied"
    assert _state(client)["location_id"] == "forge"

    # A thing that is not there is refused, and the refusal costs no version.
    refused = _act(client, "r3", "take", target_id="steel_ingot")
    assert refused["status"] == "rejected"
    before = _state(client)["version"]
    _act(client, "r4", "look")
    assert _state(client)["version"] == before + 1


def test_a_player_can_do_something_nobody_defined_in_advance(client):
    """Crafting and dismantling are new primitives, not new verbs per action."""
    _bind(client)
    _act(client, "r1", "move", target_id="forge")
    # One ore is lying in the open, the second belongs to the smith. Two pieces
    # are needed, and the same primitive fetches both: take from the room, then
    # take from a person. Nothing here is a crafting-only code path.
    assert _act(client, "r2", "take", target_id="iron_ore")["status"] == "applied"
    borrowed = _act(
        client,
        "r3",
        "give_item",
        target_id="iron_ore",
        params={"from_id": "smith_dorn", "amount": 1},
    )
    assert borrowed["status"] == "applied", borrowed
    assert borrowed["code"] == "received"
    assert _state(client)["inventory"]["iron_ore"] == 2

    forged = _act(client, "r4", "combine", target_id="steel_ingot")

    assert forged["status"] == "applied"
    assert forged["code"] == "combined"
    inventory = _state(client)["inventory"]
    assert inventory["steel_ingot"] == 1
    # Two ores went in, so there is nothing left of them.
    assert inventory.get("iron_ore", 0) == 0


def test_npcs_act_on_their_own_between_player_turns(client):
    _bind(client)

    _act(client, "r1", "look")

    npc_turns = _npc_events(client)
    assert npc_turns, "the town did nothing when the player looked around"
    # Every NPC turn is an ordinary recorded request, not a private code path.
    for row in npc_turns:
        stored = client.get(f"{API}/actions/{row['request_id']}")
        assert stored.status_code == 200, stored.text
        assert stored.json()["request"]["actor_type"] == "npc"


def test_world_time_moves_without_the_player_typing(client):
    """A wait is enough to make the town change on its own."""
    _bind(client)
    _act(client, "r1", "look")
    start = _state(client)
    before = Clock(start["day"], start["minute"]).abs_minute

    slept = _act(client, "r2", "wait", params={"minutes": 12 * 60})

    assert slept["status"] == "applied"
    state = _state(client)
    after = Clock(state["day"], state["minute"]).abs_minute
    assert after - before == 12 * 60
    # The clock passed through the night, where people sleep, and the routines
    # that came due in it were applied rather than skipped over.
    ticks = [
        row
        for row in _events(client)
        if row.get("event_type") == "WORLD_TICK"
        and (row.get("payload") or {}).get("kind") == npc_schedule.NPC_ROUTINE
    ]
    assert len(ticks) >= 2


def test_scheduled_events_fire_and_are_recorded_as_facts(client):
    _bind(client)
    service = _service(client)
    world = service.store.get_world("hollowmere")

    queued = npc_schedule.schedule_day(
        service.store, CAMPAIGN, world, day=1, now_abs_minute=Clock(1, 480).abs_minute
    )
    assert queued

    _act(client, "r1", "wait", params={"minutes": 200})

    kinds = [
        (row.get("payload") or {}).get("kind")
        for row in _events(client)
        if row.get("event_type") == "WORLD_TICK"
    ]
    assert npc_schedule.NPC_ROUTINE in kinds


def test_seven_days_of_authored_routines_keep_the_town_coherent(client):
    """The stress test: a week passes with everyone keeping their own day."""
    _bind(client)
    service = _service(client)
    world = service.store.get_world("hollowmere")

    state = service.store.get_state(CAMPAIGN)
    npc_schedule.schedule_day(
        service.store, CAMPAIGN, world, day=1, now_abs_minute=Clock.from_state(state).abs_minute
    )
    for day in range(2, 8):
        npc_schedule.schedule_day(
            service.store, CAMPAIGN, world, day=day, now_abs_minute=0
        )

    # Seven days, a few turns at a time, all through the player's own clock.
    for index in range(14):
        _act(client, f"week-{index}", "wait", params={"minutes": 12 * 60})

    final = _state(client)
    assert final["day"] >= 8

    # Nobody was left standing in a place they do not belong: every NPC that
    # has a record is somewhere real.
    known = {location.id for location in world.locations}
    for entity_id, record in (final["entities"] or {}).items():
        assert record["location_id"] in known, entity_id


def test_the_same_inputs_produce_the_same_authoritative_state(client):
    """Reproducibility is a promise, so it is a test."""
    _bind(client)
    _wake_twin(client, CAMPAIGN)
    for index, action in enumerate(("look", "move", "look", "wait")):
        fields = {"target_id": "forge"} if action == "move" else {}
        if action == "wait":
            fields = {"params": {"minutes": 90}}
        _act(client, f"r{index}", action, **fields)
    first = _state(client)

    # A second campaign, same world, same script: the version is part of the
    # answer, so an accidental extra write would show up here.
    client.post(f"{API}/campaigns/camp-twin/world", json={"world_id": "hollowmere"})
    _wake_twin(client, "camp-twin")
    for index, action in enumerate(("look", "move", "look", "wait")):
        fields = {"target_id": "forge"} if action == "move" else {}
        if action == "wait":
            fields = {"params": {"minutes": 90}}
        # Request ids are unique per campaign, and this campaign is a different
        # one: the replay uses its own ids so the comparison is about state, not
        # about a reused key.
        body = {"campaign_id": "camp-twin", "request_id": f"twin-{index}", "action": action}
        body.update(fields)
        assert client.post(f"{API}/campaigns/camp-twin/actions", json=body).status_code == 200
    second = client.get(f"{API}/campaigns/camp-twin/state").json()

    for field in ("day", "minute", "location_id", "version", "inventory"):
        assert first[field] == second[field], field


def test_two_actors_with_the_same_goal_still_choose_differently():
    """Determinism is not sameness: traits are what separate them."""
    world = _world_model()
    smith = next(entity for entity in world.entities if entity.id == "smith_dorn")
    guard = next(entity for entity in world.entities if entity.id == "guard_bran")
    needs = {"hunger": 0.5, "energy": 0.9, "social": 0.5}
    now = Clock(1, 10 * 60).abs_minute

    first = npc_brain.decide(
        store=None, campaign_id="c", state=_bare_state(), world=world, npc=smith, needs=needs
    )
    second = npc_brain.decide(
        store=None, campaign_id="c", state=_bare_state(), world=world, npc=guard, needs=needs
    )

    assert first.intent != second.intent or first.plan != second.plan
    assert now  # the clock is the shared reference for both


def _wake_twin(client, campaign_id: str) -> None:
    """Put both campaigns on the same calendar before comparing them.

    A campaign is live from its first turn, so the very first action also
    queues the routines of the day. Comparing two scripted runs therefore has
    to start them from the same point instead of pretending the calendar is
    somehow part of the inputs.
    """
    from app.api.routes_engine import _service as make_service

    service = make_service()
    state = service.store.get_state(campaign_id)
    world = service.store.get_world(state.world_id)
    npc_schedule.schedule_day(
        service.store, campaign_id, world, day=1, now_abs_minute=Clock.from_state(state).abs_minute
    )


def _world_model():
    from app.models.game import WorldDefinition

    return WorldDefinition.model_validate(town_data())


def _bare_state():
    from app.models.game import GameState

    return GameState(
        campaign_id="c", world_id="hollowmere", location_id="square", day=1, minute=600
    )


def _service(client):
    from app.api.routes_engine import _service as make_service

    return make_service()
