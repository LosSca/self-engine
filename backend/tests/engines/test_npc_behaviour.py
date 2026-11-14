"""Phase 3: NPCs that decide for themselves and act through ActionService.

The tests are deliberately about *behaviour*, not wiring: an NPC that sees
nobody leaves; an NPC that saw a theft re-plans; a decision is reproducible; and
nothing an NPC does can bypass the rules engine.
"""
from __future__ import annotations

import pytest

from app.engines import npc_brain, npc_goals, npc_schedule
from app.engines.event_engine import apply_effects, entity_location
from app.engines.rules_engine import RulesEngine
from app.models.game import (
    EntityDef,
    EntityState,
    ExitDef,
    GameState,
    LocationDef,
    RoutineEntryDef,
    WorldDefinition,
)


def _world(**overrides) -> WorldDefinition:
    locations = [
        LocationDef(id="square", name="Square", exits=[ExitDef(target_id="forge"), ExitDef(target_id="house")]),
        LocationDef(id="forge", name="Forge", exits=[ExitDef(target_id="square")]),
        LocationDef(id="house", name="House", exits=[ExitDef(target_id="square")]),
    ]
    smith = EntityDef(
        id="smith",
        name="Smith",
        location_id="house",
        schedule=[
            RoutineEntryDef(start="09:00", activity="work", location_id="forge", minutes=480),
            RoutineEntryDef(start="18:00", activity="sleep", location_id="house", minutes=720),
        ],
        traits={"diligence": 0.9},
    )
    defaults = dict(
        world_id="w1",
        name="Town",
        start_location_id="square",
        locations=locations,
        entities=[smith],
    )
    defaults.update(overrides)
    return WorldDefinition(**defaults)


def test_an_authored_routine_compiles_to_absolute_minutes():
    from app.engines.game_clock import Clock, midnight

    world = _world()
    entries = npc_schedule.routine_entries(world, day=1)
    assert [entry["due_abs_minute"] for entry in entries] == [
        midnight(1, 9 * 60),
        midnight(1, 18 * 60),
    ]
    assert entries[0]["payload"]["npc_id"] == "smith"
    assert entries[0]["payload"]["activity"] == "work"
    # Day 2 is exactly one day later: the compiler is pure arithmetic, so the
    # same world always produces the same schedule.
    day_two = npc_schedule.routine_entries(world, day=2)
    assert day_two[0]["due_abs_minute"] == entries[0]["due_abs_minute"] + 1440
    assert Clock(day=2, minute=540).abs_minute == day_two[0]["due_abs_minute"]


def test_the_slot_in_force_is_answered_by_lookup_not_by_replay():
    from app.engines.game_clock import Clock

    world = _world()
    npc = world.entities[0]

    def at(day: int, minute: int):
        return npc_brain.activity_at(world, npc, Clock(day=day, minute=minute).abs_minute)

    assert at(1, 10 * 60) == ("work", "forge")
    assert at(1, 19 * 60) == ("sleep", "house")
    # Before the first slot of a day, yesterday's last slot still applies:
    # "nothing scheduled yet" is not "doing nothing".
    assert at(2, 60) == ("sleep", "house")
    # And on the very first morning, before any slot, there is no answer yet.
    assert at(1, 7 * 60) == ("", "")


def test_hunger_outranks_diligence_when_it_is_bad_enough():
    npc = _world().entities[0]
    idle = npc_goals.score_intents(
        npc, {"hunger": 0.9, "energy": 0.9, "social": 0.9}, has_work=True, can_work_here=True
    )
    assert idle[0].name == "work"
    starving = npc_goals.score_intents(
        npc, {"hunger": 0.05, "energy": 0.9, "social": 0.9}, has_work=True, can_work_here=True
    )
    assert starving[0].name == "eat"


def test_the_same_state_always_chooses_the_same_intent():
    npc = _world().entities[0]
    needs = {"hunger": 0.5, "energy": 0.5, "social": 0.5}
    first = npc_goals.choose_intent(npc_goals.score_intents(npc, needs, has_work=True))
    second = npc_goals.choose_intent(npc_goals.score_intents(npc, needs, has_work=True))
    assert first.name == second.name
    assert first.score == second.score


def test_an_npc_only_acts_on_what_it_witnessed(monkeypatch):
    npc = _world().entities[0]
    state = GameState(campaign_id="c1", world_id="w1", location_id="square")

    class Row:
        def __init__(self, witnessed):
            self.payload = {"action": "take", "code": "item_taken"}
            self.witnessed_by = witnessed

    class Store:
        def __init__(self, rows):
            self._rows = rows

        def list_events(self, campaign_id, limit=200):
            return self._rows

    unseen = npc_brain.perceive(Store([Row([])]), "c1", npc)
    assert unseen["something_is_wrong"] is False

    seen = npc_brain.perceive(Store([Row(["Smith"])]), "c1", npc)
    assert seen["something_is_wrong"] is True

    decision = npc_brain.decide(
        store=Store([Row(["Smith"])]),
        campaign_id="c1",
        state=state,
        world=_world(),
        npc=npc,
    )
    assert decision.intent == "investigate"
    assert "noticed something wrong" in decision.reason


def test_a_routine_is_activity_and_rest_repairs_the_need():
    world = _world()
    state = GameState(campaign_id="c1", world_id="w1", location_id="square")
    from app.services.npc_turns import routine_handler

    call = routine_handler(None)
    entry = npc_schedule.routine_entries(world, day=1)[1]  # 18:00 sleep
    # A routine has to target a materialised NPC: an entity that has never
    # changed has no runtime record to hang the activity on.
    state.entities["smith"] = EntityState(entity_id="smith", location_id="house")
    new_state, events, _scheduled = call(None, state, entry)
    record = new_state.entities["smith"]
    assert record.activity == "sleep"
    assert record.needs["energy"] >= 0.8
    assert events[0]["event_type"] == "npc.routine"


def test_an_npc_moves_through_the_rules_engine_not_around_it():
    world = _world()
    state = GameState(campaign_id="c1", world_id="w1", location_id="square")
    outcome = RulesEngine().evaluate(
        world, state, "travel", "forge", {"actor_id": "smith"}
    )
    assert outcome.ok
    applied = apply_effects(state.model_copy(deep=True), world, outcome.effects)
    assert all(item.ok for item in applied)
    moved = applied[-1].effect
    assert moved.kind == "entity_travel"
    assert moved.target == "smith"


def test_an_npc_cannot_walk_through_a_wall():
    world = _world()
    state = GameState(campaign_id="c1", world_id="w1", location_id="square")
    outcome = RulesEngine().evaluate(
        world, state, "travel", "nowhere", {"actor_id": "smith"}
    )
    assert not outcome.ok
    assert outcome.code == "unknown_location"


def test_the_plan_only_contains_actions_the_rules_engine_knows():
    from app.engines.action_registry import get_action

    world = _world()
    npc = world.entities[0]
    npc.traits["diligence"] = 1.0
    plan = npc_goals.plan_for(
        "work", npc=npc, location_id="house", work_location_id="forge"
    )
    assert plan[0]["action"] == "travel"
    assert get_action(plan[0]["action"]) is not None


def test_a_world_without_routines_never_schedules_anything():
    world = _world(entities=[EntityDef(id="drudge", name="Drudge", location_id="house")])
    assert npc_schedule.routine_entries(world, day=1) == []
