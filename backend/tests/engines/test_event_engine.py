"""Structured event engine: conditions, effects, cascades, simulation."""
from app.engines.event_engine import (
    Fact,
    apply_effect,
    apply_effects,
    condition_holds,
    diff_states,
    run_triggered_events,
    simulate_event,
    validate_event,
)
from app.models.game import Condition, Effect, GameEventDef
from app.services.world_service import build_initial_state
from tests.support import village_world


def _setup():
    world = village_world()
    return world, build_initial_state(world, "camp-1")


def _event(**overrides):
    base = dict(
        id="evt",
        name="Test Event",
        enabled=True,
        trigger="action",
        trigger_action="open",
        conditions=[],
        effects=[],
        priority=0,
        repeat="once",
    )
    base.update(overrides)
    return GameEventDef(**base)


# ------------------------------------------------------------------ conditions
def test_has_item_condition():
    world, state = _setup()
    condition = Condition(kind="has_item", target="iron_key")
    assert condition_holds(state, world, condition) is False
    state.inventory["iron_key"] = 1
    assert condition_holds(state, world, condition) is True


def test_door_open_condition_respects_state_over_default():
    world, state = _setup()
    condition = Condition(kind="door_open", target="warehouse_door", value=True)
    assert condition_holds(state, world, condition) is False
    state.doors["warehouse_door"] = True
    assert condition_holds(state, world, condition) is True


def test_quest_status_condition_and_unknown_quest():
    world, state = _setup()
    active = Condition(kind="quest_status", target="open_the_warehouse", value="active")
    assert condition_holds(state, world, active) is True
    unknown = Condition(kind="quest_status", target="ghost_quest", value="active")
    assert condition_holds(state, world, unknown) is False


def test_flag_condition_uses_operators():
    world, state = _setup()
    state.flags["reputation"] = 3
    assert condition_holds(state, world, Condition(kind="flag", target="reputation", op="gte", value=3))
    assert not condition_holds(state, world, Condition(kind="flag", target="reputation", op="gt", value=3))


# --------------------------------------------------------------------- effects
def test_give_item_moves_item_from_ground_to_inventory():
    world, state = _setup()
    applied = apply_effect(state, world, Effect(kind="give_item", target="iron_key"))
    assert applied.ok
    assert state.inventory["iron_key"] == 1
    assert "iron_key" not in state.item_locations
    assert applied.facts[0].kind == "item_gained"


def test_remove_item_beyond_inventory_fails_closed():
    world, state = _setup()
    applied = apply_effect(state, world, Effect(kind="remove_item", target="iron_key", amount=2))
    assert not applied.ok
    assert "only 0 held" in applied.error
    assert "iron_key" not in state.inventory


def test_unknown_effect_targets_are_rejected():
    world, state = _setup()
    assert not apply_effect(state, world, Effect(kind="give_item", target="ghost_item")).ok
    assert not apply_effect(state, world, Effect(kind="open_door", target="ghost_door")).ok
    assert not apply_effect(state, world, Effect(kind="move_player", target="ghost_place")).ok
    assert not apply_effect(state, world, Effect(kind="quest_complete", target="ghost_quest")).ok


def test_quest_progress_rejects_unknown_objective():
    world, state = _setup()
    applied = apply_effect(
        state,
        world,
        Effect(kind="quest_progress", target="open_the_warehouse", value="ghost_objective"),
    )
    assert not applied.ok
    assert "no objective" in applied.error


def test_quest_progress_caps_at_required_count():
    world, state = _setup()
    applied = apply_effect(
        state,
        world,
        Effect(kind="quest_progress", target="open_the_warehouse", value="find_key", amount=9),
    )
    assert applied.ok
    assert state.quests["open_the_warehouse"].objectives["find_key"] == 1


def test_apply_effects_reports_every_result():
    world, state = _setup()
    results = apply_effects(
        state,
        world,
        [Effect(kind="set_flag", target="a"), Effect(kind="remove_item", target="iron_key")],
    )
    assert [item.ok for item in results] == [True, False]


# ---------------------------------------------------------------------- firing
def test_event_fires_once_and_is_skipped_on_repeat():
    world, state = _setup()
    state.doors["warehouse_door"] = True
    new_state, executions, facts = run_triggered_events(
        world, state, "open", "warehouse_door", []
    )
    fired = [e for e in executions if e.status == "fired"]
    assert [e.event_id for e in fired] == ["keeper_alert"]
    assert new_state.flags["warehouse_opened"] is True
    assert any(fact.kind == "flag" for fact in facts)
    assert "keeper_alert" in new_state.fired_events

    again_state, again_executions, _ = run_triggered_events(
        world, new_state, "open", "warehouse_door", []
    )
    assert again_executions[0].status == "skipped"
    assert again_state.flags["warehouse_opened"] is True


def test_events_fire_in_priority_order():
    world, state = _setup()
    world.events = [
        _event(id="low", priority=1, effects=[Effect(kind="set_flag", target="low")]),
        _event(id="high", priority=50, effects=[Effect(kind="set_flag", target="high")]),
    ]
    new_state, executions, _ = run_triggered_events(world, state, "open", "warehouse_door", [])
    assert [e.event_id for e in executions] == ["high", "low"]
    assert new_state.flags == {"high": None, "low": None}


def test_event_with_unmet_condition_does_not_fire():
    world, state = _setup()
    new_state, executions, _ = run_triggered_events(world, state, "open", "warehouse_door", [])
    assert executions == []
    assert new_state.flags == {}


def test_failing_event_leaves_state_untouched_and_is_recorded():
    world, state = _setup()
    world.events = [
        _event(
            id="broken",
            effects=[
                Effect(kind="set_flag", target="should_not_stick"),
                Effect(kind="remove_item", target="iron_key"),
            ],
        )
    ]
    before = state.model_dump_json()
    new_state, executions, _ = run_triggered_events(world, state, "open", "warehouse_door", [])
    assert [e.status for e in executions] == ["failed"]
    assert "only 0 held" in executions[0].reason
    assert new_state.model_dump_json() == before
    assert "broken" not in new_state.fired_events


def test_event_trigger_target_must_match():
    world, state = _setup()
    new_state, executions, _ = run_triggered_events(world, state, "open", "other_door", [])
    assert executions == []
    assert new_state.flags == {}


# ------------------------------------------------------------------ validation
def test_validate_event_reports_unknown_effect_target():
    world, _ = _setup()
    event = _event(effects=[Effect(kind="open_door", target="ghost_door")])
    errors = validate_event(world, event)
    assert any("ghost_door" in error for error in errors), errors


def test_validate_event_requires_trigger_action():
    world, _ = _setup()
    errors = validate_event(world, _event(trigger_action=None))
    assert any("trigger_action" in error for error in errors), errors


# ------------------------------------------------------------------ simulation
def test_simulate_does_not_mutate_state():
    world, state = _setup()
    state.doors["warehouse_door"] = True
    before = state.model_dump_json()
    report = simulate_event(world, state, "keeper_alert")
    assert report["status"] == "would_fire"
    assert report["diff"]["flags"]["after"] == {"warehouse_opened": True}
    assert state.model_dump_json() == before


def test_simulate_reports_not_applicable_when_conditions_fail():
    world, state = _setup()
    report = simulate_event(world, state, "keeper_alert")
    assert report["status"] == "not_applicable"
    assert report["diff"] == {}


def test_simulate_reports_disabled_and_unknown_events():
    world, state = _setup()
    world.events[0].enabled = False
    assert simulate_event(world, state, "keeper_alert")["status"] == "disabled"
    assert simulate_event(world, state, "ghost_event")["status"] == "not_found"


def test_simulate_reports_invalid_event():
    world, state = _setup()
    world.events[0].effects = [Effect(kind="open_door", target="ghost_door")]
    report = simulate_event(world, state, "keeper_alert")
    assert report["status"] == "invalid"
    assert any("ghost_door" in error for error in report["errors"])


def test_diff_states_is_empty_for_identical_states():
    world, state = _setup()
    assert diff_states(state, state.model_copy(deep=True)) == {}
