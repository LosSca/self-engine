"""Tests for the in-game clock: it moves only when the player acts."""
from __future__ import annotations

import asyncio

import pytest

from app.db.game_state_store import GameStateStore
from app.engines import game_clock
from app.engines.action_registry import get_action
from app.engines.game_clock import Clock, DEFAULT_ACTION_MINUTES, advance_state
from app.models.game import (
    ActionRequest,
    EntityDef,
    ExitDef,
    GameState,
    ItemDef,
    LocationDef,
    WorldDefinition,
)
from app.services.action_service import ActionService
from app.services.narrative_service import TemplateNarrator


def _world() -> WorldDefinition:
    return WorldDefinition(
        world_id="w1",
        name="Clock World",
        start_location_id="square",
        locations=[
            LocationDef(
                id="square",
                name="Square",
                description="A cobbled square.",
                exits=[ExitDef(target_id="road")],
            ),
            LocationDef(id="road", name="Road", description="A dusty road.", exits=[ExitDef(target_id="square")]),
        ],
        items=[ItemDef(id="bread", name="Bread", description="A loaf.", price=5)],
        entities=[
            EntityDef(
                id="merchant",
                name="Merchant",
                location_id="square",
                description="A trader.",
                shop_items=["bread"],
                attributes={"hours": [{"opens_minute": 540, "closes_minute": 720}]},
            ),
        ],
    )


def _store(tmp_path, clock_paused=None, *, minute=480) -> tuple[GameStateStore, ActionService]:
    store = GameStateStore(str(tmp_path / "gs.db"))
    store.save_world(_world())
    store.bind_campaign(GameState(campaign_id="c1", world_id="w1", location_id="square", minute=minute, currency=100))
    service = ActionService(store, narrator=TemplateNarrator(), clock_paused=clock_paused, history_provider=lambda cid, st: [])
    return store, service


def _ask(service, action, *, target=None, params=None, request_id=None, campaign_id="c1"):
    return asyncio.run(service.execute(ActionRequest(
        campaign_id=campaign_id,
        request_id=request_id or f"r-{action}",
        action=action,
        target_id=target,
        params=params or {},
        narrate=False,
    )))


class TestClockArithmetic:
    def test_the_world_starts_on_a_known_morning(self):
        assert Clock().label == "Day 1, 08:00"
        assert Clock().time_of_day == "dawn"

    def test_advancing_rolls_over_midnight(self):
        clock = Clock(day=1, minute=23 * 60 + 50).advance(20)
        assert (clock.day, clock.minute) == (2, 10)
        assert clock.label == "Day 2, 00:10"

    def test_a_paused_clock_never_moves(self):
        clock = Clock().pause()
        assert clock.advance(10_000) is clock
        assert clock.abs_minute == Clock().abs_minute

    def test_old_saves_read_as_the_default_morning(self):
        state = GameState.model_validate({"campaign_id": "c1", "world_id": "w1"})
        assert (state.day, state.minute) == (1, 480)
        saved = GameState.model_validate({"campaign_id": "c1", "world_id": "w1", "day": 4, "minute": 61})
        assert Clock.from_state(saved).label == "Day 4, 01:01"

class TestDuration:
    def test_each_verb_declares_its_cost(self):
        assert get_action("look").minutes == 0
        assert get_action("talk").minutes == 0
        assert get_action("move").minutes == 10
        assert game_clock.schedule_minutes(get_action("move")) == 10

    def test_an_undeclared_verb_falls_back_to_the_table(self):
        assert get_action("use").minutes is None
        assert game_clock.schedule_minutes(get_action("use")) == DEFAULT_ACTION_MINUTES["use"]
        assert game_clock.duration_minutes("nobody-registered-this") == game_clock.UNKNOWN_ACTION_MINUTES

    def test_an_explicit_param_wins(self):
        assert game_clock.duration_minutes("move", {"minutes": 42}) == 42
        assert game_clock.duration_minutes("move", {"duration_minutes": "7"}) == 7
        assert game_clock.duration_minutes("wait", {"duration": "2 hours"}) == 120
        assert game_clock.duration_minutes("wait", {"minutes": False}) == game_clock.UNKNOWN_ACTION_MINUTES

    def test_duration_text_is_narrow_on_purpose(self):
        assert game_clock.parse_duration("30 min") == 30
        assert game_clock.parse_duration("2 days") == 2880
        assert game_clock.parse_duration("a while later") is None
        assert game_clock.parse_duration("") is None


class TestActionAdvancesTheClock:
    def test_moving_costs_ten_minutes_and_commits_them(self, tmp_path):
        store, service = _store(tmp_path)
        result = _ask(service, "move", target="road")
        assert result.status == "applied"
        assert result.minutes_spent == 10
        assert result.clock == "Day 1, 08:10"
        assert (result.state.day, result.state.minute) == (1, 490)
        assert (store.get_state("c1").day, store.get_state("c1").minute) == (1, 490)

    def test_looking_around_takes_no_time(self, tmp_path):
        _unused, service = _store(tmp_path)
        result = _ask(service, "look")
        assert result.status == "applied"
        assert result.minutes_spent == 0
        assert result.clock == "Day 1, 08:00"
        assert result.state.minute == 480

    def test_a_rejected_action_does_not_move_the_clock(self, tmp_path):
        store, service = _store(tmp_path)
        result = _ask(service, "move", target="nowhere")
        assert result.status == "rejected"
        assert result.minutes_spent == 0
        assert store.get_state("c1").minute == 480

    def test_a_duplicate_request_does_not_buy_more_time(self, tmp_path):
        store, service = _store(tmp_path)
        first = _ask(service, "move", target="road", request_id="r-1")
        second = _ask(service, "move", target="road", request_id="r-1")
        assert first.minutes_spent == 10
        assert second.status == "duplicate"
        assert store.get_state("c1").minute == 490

    def test_a_paused_world_does_not_age(self, tmp_path):
        store, service = _store(tmp_path, clock_paused=lambda campaign_id: True)
        result = _ask(service, "move", target="road")
        assert result.status == "applied"
        assert result.minutes_spent == 0
        assert store.get_state("c1").minute == 480

    def test_a_broken_pause_probe_does_not_freeze_the_world(self, tmp_path):
        def boom(campaign_id):
            raise RuntimeError("no session")

        store, service = _store(tmp_path, clock_paused=boom)
        result = _ask(service, "move", target="road")
        assert result.minutes_spent == 10


class TestMerchantHours:
    def test_a_shop_is_closed_outside_its_hours(self, tmp_path):
        _unused, service = _store(tmp_path, minute=8 * 60)
        result = _ask(service, "buy", target="bread")
        assert result.status == "rejected"
        assert result.code == "shop_closed"

    def test_the_same_shop_is_open_at_noon(self, tmp_path):
        _unused, service = _store(tmp_path, minute=11 * 60)
        result = _ask(service, "buy", target="bread")
        assert result.status != "rejected" or result.code != "shop_closed"

    def test_an_entity_without_hours_is_always_open(self):
        shop = EntityDef(id="m", name="M", location_id="square")
        assert game_clock.open_now(shop, Clock(day=3, minute=3 * 60))
        assert game_clock.is_open_now(22 * 60, 2 * 60, 23 * 60)
        assert not game_clock.is_open_now(22 * 60, 2 * 60, 12 * 60)
