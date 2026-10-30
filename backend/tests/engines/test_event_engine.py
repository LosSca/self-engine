"""Structured event engine: conditions, effects, cascades, simulation."""
import pytest

from app.db.game_state_store import GameStateStore
from app.engines.event_engine import (
    Fact,
    apply_effect,
    apply_effects,
    condition_holds,
    diff_states,
    entity_item_count,
    run_triggered_events,
    simulate_event,
    validate_event,
)
from app.models.game import (
    ActionRequest,
    CombatantState,
    Condition,
    Effect,
    EntityDef,
    EntityState,
    EventBranchDef,
    GameEventDef,
    GameState,
)
from app.services.action_service import ActionService
from app.services.world_service import build_initial_state
from tests.support import village_world


def _setup():
    world = village_world()
    return world, build_initial_state(world, "camp-1")


def _setup_with_entity():
    world, state = _setup()
    world.entities.append(
        EntityDef(id="guard", name="Guard", location_id="village_square")
    )
    return world, state


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


@pytest.mark.parametrize("kind", ["door_open", "at_location"])
@pytest.mark.parametrize("actual", [True, False])
@pytest.mark.parametrize("value", [True, False, None])
@pytest.mark.parametrize("op", ["eq", "ne", "gte", "lte", "gt", "lt"])
def test_boolean_conditions_honor_comparisons(kind, actual, value, op):
    world, state = _setup()
    target = "warehouse_door" if kind == "door_open" else "village_square"
    state.doors["warehouse_door"] = actual
    state.location_id = "village_square" if actual else "forest_edge"
    expected = True if value is None else value
    comparisons = {
        "eq": actual == expected,
        "ne": actual != expected,
        "gte": actual >= expected,
        "lte": actual <= expected,
        "gt": actual > expected,
        "lt": actual < expected,
    }
    condition = Condition(kind=kind, target=target, value=value, op=op)
    assert condition_holds(state, world, condition) is comparisons[op]


@pytest.mark.parametrize("op,expected", [("eq", False), ("ne", True), ("gte", False), ("lt", True)])
def test_at_location_compares_boolean_actual_to_authored_value(op, expected):
    world, state = _setup()
    condition = Condition(kind="at_location", target="village_square", op=op, value=2)
    assert condition_holds(state, world, condition) is expected


@pytest.mark.parametrize("value", ["village_square", "forest_edge"])
@pytest.mark.parametrize("op", ["eq", "ne", "gte", "lte", "gt", "lt"])
def test_entity_at_compares_runtime_location(value, op):
    world, state = _setup_with_entity()
    state.entities["guard"] = EntityState(entity_id="guard", location_id="forest_edge")
    expected = {"eq": value == "forest_edge", "ne": value != "forest_edge"}.get(op, False)
    condition = Condition(kind="entity_at", target="guard", value=value, op=op)
    assert condition_holds(state, world, condition) is expected


@pytest.mark.parametrize("stale_record", [True, False])
@pytest.mark.parametrize("op", ["eq", "ne", "gte", "lte", "gt", "lt"])
@pytest.mark.parametrize(
    "kind,value",
    [
        ("entity_alive", True),
        ("entity_alive", False),
        ("combatant_alive", True),
        ("combatant_alive", False),
        ("combatant_hp", 0),
        ("combatant_hp", -1),
        ("entity_at", "village_square"),
    ],
)
def test_unknown_entity_predicates_fail_closed(kind, value, op, stale_record):
    world, state = _setup()
    if stale_record:
        state.entities["ghost"] = EntityState(entity_id="ghost", location_id="village_square")
        state.combatants["ghost"] = CombatantState(entity_id="ghost")
    condition = Condition(kind=kind, target="ghost", value=value, op=op)
    assert condition_holds(state, world, condition) is False


def test_known_unseeded_entity_is_alive_without_materializing_it():
    world, state = _setup_with_entity()
    before = state.model_dump()
    assert condition_holds(state, world, Condition(kind="entity_alive", target="guard"))
    assert state.model_dump() == before


@pytest.mark.parametrize("value", [None, True, 1, [], ["village_square"], {}, {"id": "village_square"}])
def test_bad_entity_location_shapes_fail_closed(value):
    world, state = _setup_with_entity()
    condition = Condition(kind="entity_at", target="guard", value=value, op="ne")
    effect = Effect(kind="move_entity", target="guard", value=value)
    event = _event(conditions=[condition], effects=[effect])
    errors = validate_event(world, event)
    assert len(errors) == 2
    assert all("location id (string)" in error for error in errors)
    assert condition_holds(state, world, condition) is False
    before = state.model_dump()
    result = apply_effect(state, world, effect)
    assert not result.ok
    assert "location id (string)" in result.error
    assert state.model_dump() == before


@pytest.mark.parametrize("value", ["bad", "", "1.5", [], {}, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("seeded", [True, False])
def test_malformed_combat_hp_is_rejected_without_mutation(value, seeded):
    world, state = _setup_with_entity()
    if seeded:
        assert apply_effect(state, world, Effect(kind="combat_hp", target="guard", value=8)).ok
    effect = Effect(kind="combat_hp", target="guard", value=value)
    errors = validate_event(world, _event(effects=[effect]))
    assert any("combat_hp requires a numeric value" in error for error in errors)
    before = state.model_dump()
    direct = apply_effect(state, world, effect)
    batch = apply_effects(state, world, [effect, Effect(kind="emit", target="after_failure")])
    assert not direct.ok
    assert "combat_hp requires a numeric value" in direct.error
    assert direct.facts == []
    assert [result.ok for result in batch] == [False, True]
    assert state.model_dump() == before


@pytest.mark.parametrize("value,hp", [(None, 0), (0, 0), (3, 3), (3.5, 3), ("4", 4), (-5, 0), (99, 10)])
def test_convertible_combat_hp_values_remain_supported(value, hp):
    world, state = _setup_with_entity()
    effect = Effect(kind="combat_hp", target="guard", value=value)
    assert validate_event(world, _event(effects=[effect])) == []
    assert apply_effect(state, world, effect).ok
    assert state.combatants["guard"].hp == hp
    assert state.entities["guard"].hp == hp


def test_malformed_combat_effect_rolls_back_the_entire_event():
    world, state = _setup_with_entity()
    world.events = [_event(effects=[
        Effect(kind="set_flag", target="partial", value=True),
        Effect(kind="combat_hp", target="guard", value="bad"),
        Effect(kind="give_item", target="iron_key"),
    ])]
    before = state.model_dump()
    after, executions, facts = run_triggered_events(world, state, "open", None, [])
    assert [(entry.event_id, entry.status) for entry in executions] == [("evt", "failed")]
    assert "combat_hp requires a numeric value" in executions[0].reason
    assert facts == []
    assert after.model_dump() == state.model_dump() == before


@pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError])
def test_effect_batch_does_not_hide_programming_errors(monkeypatch, error_type):
    world, state = _setup_with_entity()

    def broken_seed(*args):
        raise error_type("unexpected implementation failure")

    monkeypatch.setattr("app.engines.event_engine.entity_state_for", broken_seed)
    with pytest.raises(error_type, match="unexpected implementation failure"):
        apply_effects(state, world, [Effect(kind="combat_hp", target="guard", value=1)])


def test_reverse_priority_cascade_settles_beyond_two_passes():
    world, state = _setup()
    world.events = [
        _event(
            id=f"step_{index}",
            trigger="state_change",
            priority=index,
            conditions=[Condition(kind="flag", target=f"step_{index - 1}")] if index else [],
            effects=[Effect(kind="set_flag", target=f"step_{index}", value=True)],
        )
        for index in reversed(range(5))
    ]
    before = state.model_dump()
    after, executions, facts = run_triggered_events(world, state, "look", None, [])
    expected = [f"step_{index}" for index in range(5)]
    assert [entry.event_id for entry in executions] == expected
    assert all(entry.status == "fired" for entry in executions)
    assert after.fired_events == expected
    assert [fact.target for fact in facts] == expected
    assert state.model_dump() == before


def test_equal_priority_events_are_ordered_by_id():
    world, state = _setup()
    world.events = [_event(id="z"), _event(id="a"), _event(id="m")]
    _, executions, _ = run_triggered_events(world, state, "open", None, [])
    assert [entry.event_id for entry in executions] == ["a", "m", "z"]


def test_always_events_execute_once_per_action_and_enter_history_once():
    world, state = _setup()
    world.events = [
        _event(
            id="always",
            priority=10,
            repeat="always",
            effects=[Effect(kind="give_item", target="blessing_token")],
        ),
        _event(id="once", effects=[Effect(kind="set_flag", target="ready", value=True)]),
    ]
    for turn in range(1, 4):
        state, executions, facts = run_triggered_events(world, state, "open", None, [])
        state.turn += 1
        assert state.inventory["blessing_token"] == turn
        assert state.fired_events == ["always", "once"]
        assert [(entry.event_id, entry.status) for entry in executions] == [
            ("always", "fired"), ("once", "fired" if turn == 1 else "skipped")
        ]
        assert sum(fact.kind == "item_gained" for fact in facts) == 1


def test_failed_events_are_not_retried_during_cascades_or_shared_passes():
    world, state = _setup()
    world.events = [
        _event(
            id="fails_first",
            priority=10,
            effects=[Effect(kind="remove_item", target="iron_key")],
        ),
        _event(id="supply", effects=[Effect(kind="give_item", target="iron_key")]),
    ]
    processed = set()
    after, executions, facts = run_triggered_events(world, state, "open", None, [], processed=processed)
    assert [(entry.event_id, entry.status) for entry in executions] == [
        ("fails_first", "failed"), ("supply", "fired")
    ]
    assert processed == {"fails_first", "supply"}
    assert after.inventory["iron_key"] == 1
    assert after.fired_events == ["supply"]
    assert facts == [Fact("item_gained", "iron_key", 1)]
    again, executions, facts = run_triggered_events(world, after, "open", None, [], processed=processed)
    assert executions == facts == []
    assert again.model_dump() == after.model_dump()


def test_shared_processed_set_keeps_ineligible_events_available_for_rewards():
    world, state = _setup()
    world.events = [
        _event(
            id="gated",
            priority=10,
            conditions=[Condition(kind="flag", target="reward_received")],
            effects=[Effect(kind="set_flag", target="settled", value=True)],
        ),
        _event(id="always", repeat="always", effects=[Effect(kind="give_item", target="iron_key")]),
        _event(id="old"),
    ]
    state.fired_events = ["old"]
    processed = set()
    state, first, _ = run_triggered_events(world, state, "open", None, [], processed=processed)
    assert processed == {"always", "old"}
    assert [(entry.event_id, entry.status) for entry in first] == [("always", "fired"), ("old", "skipped")]
    state.flags["reward_received"] = True
    state, second, _ = run_triggered_events(world, state, "open", None, [], processed=processed)
    assert [(entry.event_id, entry.status) for entry in second] == [("gated", "fired")]
    assert state.inventory["iron_key"] == 1
    assert state.flags["settled"] is True
    assert state.fired_events == ["old", "always", "gated"]
    assert processed == {"old", "always", "gated"}


def test_failing_scratch_effects_cannot_enable_other_events():
    world, state = _setup()
    world.events = [
        _event(id="gated", priority=10, conditions=[Condition(kind="flag", target="partial")]),
        _event(id="broken", effects=[
            Effect(kind="set_flag", target="partial", value=True),
            Effect(kind="remove_item", target="iron_key"),
        ]),
    ]
    after, executions, facts = run_triggered_events(world, state, "open", None, [])
    assert [(entry.event_id, entry.status) for entry in executions] == [("broken", "failed")]
    assert after.model_dump() == state.model_dump()
    assert facts == []


def test_simulate_once_history_is_skipped_without_evaluating_conditions(monkeypatch):
    world, state = _setup()
    state.fired_events.append("keeper_alert")
    before = state.model_dump()

    def unexpected_condition(*args):
        pytest.fail("already-fired once events must not evaluate conditions")

    monkeypatch.setattr("app.engines.event_engine.condition_holds", unexpected_condition)
    first = simulate_event(world, state, "keeper_alert")
    second = simulate_event(world, state, "keeper_alert")
    assert first == second
    assert first["status"] == "skipped"
    assert first["reason"] == "already fired (repeat=once)"
    assert first["effects_applied"] == []
    assert first["diff"] == {}
    assert first["state_before"] == first["state_after"] == before
    assert state.model_dump() == before


def test_repeated_simulation_is_pure_and_preview_history_matches_execution():
    world, state = _setup()
    state.doors["warehouse_door"] = True
    before = state.model_dump()
    first = simulate_event(world, state, "keeper_alert")
    assert simulate_event(world, state, "keeper_alert") == first
    assert first["status"] == "would_fire"
    assert state.model_dump() == before
    preview = GameState.model_validate(first["state_after"])
    assert simulate_event(world, preview, "keeper_alert")["status"] == "skipped"
    actual, _, _ = run_triggered_events(world, state, "open", "warehouse_door", [])
    assert first["state_after"] == actual.model_dump()


def test_simulate_always_event_ignores_history_without_duplicating_it():
    world, state = _setup()
    state.fired_events = ["key_pickup_noted"]
    before = state.model_dump()
    report = simulate_event(world, state, "key_pickup_noted")
    assert report["status"] == "would_fire"
    assert report["state_after"]["fired_events"] == ["key_pickup_noted"]
    assert state.model_dump() == before


@pytest.mark.parametrize("invalid_part", ["condition", "effect", "combat_hp"])
def test_simulate_validates_before_evaluating_conditions(monkeypatch, invalid_part):
    world, state = _setup_with_entity()
    event = _event()
    if invalid_part == "condition":
        event.conditions = [Condition(kind="entity_at", target="guard", value={"id": "village_square"})]
    elif invalid_part == "effect":
        event.effects = [Effect(kind="move_entity", target="guard", value=["village_square"])]
    else:
        event.effects = [Effect(kind="combat_hp", target="guard", value="bad")]
    if not event.conditions:
        event.conditions = [Condition(kind="at_location", target="village_square")]
    world.events = [event]
    before = state.model_dump()

    def unexpected_condition(*args):
        pytest.fail("invalid events must not evaluate conditions")

    monkeypatch.setattr("app.engines.event_engine.condition_holds", unexpected_condition)
    report = simulate_event(world, state, "evt")
    assert report["status"] == "invalid"
    assert report["errors"]
    assert report["conditions_met"] is False
    assert report["effects_applied"] == []
    assert report["diff"] == {}
    assert report["state_before"] == report["state_after"] == before
    assert state.model_dump() == before


@pytest.mark.parametrize("source", ["event", "item"])
async def test_persisted_malformed_effect_fails_closed_during_actions(tmp_path, monkeypatch, source):
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    store = GameStateStore(str(tmp_path / "state.db"))
    try:
        world, state = _setup_with_entity()
        world.quests = []
        state.quests = {}
        effects = [
            Effect(kind="set_flag", target="partial", value=True),
            Effect(kind="combat_hp", target="guard", value="bad"),
        ]
        if source == "event":
            world.events = [_event(trigger_action="look", effects=effects)]
            action, target = "look", None
        else:
            world.events = []
            world.items[0].use_effects = effects
            state.inventory["iron_key"] = 1
            action, target = "use", "iron_key"
        store.save_world(world)
        store.bind_campaign(state)
        before = state.model_dump()
        result = await ActionService(store).execute(ActionRequest(
            campaign_id=state.campaign_id,
            request_id="malformed_effect",
            action=action,
            target_id=target,
            narrate=False,
        ))
        after = store.get_state(state.campaign_id)
        assert "partial" not in after.flags
        assert after.combatants == {}
        assert after.fired_events == []
        if source == "event":
            assert result.status == "applied"
            assert [(entry.event_id, entry.status) for entry in result.events_fired] == [("evt", "failed")]
            assert "combat_hp requires a numeric value" in result.events_fired[0].reason
            assert after.turn == after.version == 1
        else:
            assert result.status == "rejected"
            assert result.code == "effect_failed"
            assert "combat_hp requires a numeric value" in result.message
            assert after.model_dump() == before
    finally:
        store.close()


# ------------------------------------------------- follow-ups and repeat policy
def test_followup_event_runs_even_when_its_trigger_does_not_match():
    world, state = _setup()
    world.events = [
        _event(id="caller", trigger_action="take", effects=[Effect(kind="set_flag", target="called", value=True)]),
        _event(
            id="callee",
            trigger_action="open",
            trigger_target="warehouse_door",
            effects=[Effect(kind="set_flag", target="callee_ran", value=True)],
        ),
    ]
    world.events[0].followup_events = ["callee"]
    state, executions, _ = run_triggered_events(world, state, "take", "iron_key", [])
    assert [e.event_id for e in executions if e.status == "fired"] == ["caller", "callee"]
    assert state.flags == {"called": True, "callee_ran": True}


def test_failure_event_runs_without_committing_the_parent():
    world, state = _setup()
    world.events = [
        _event(
            id="broken",
            trigger_action="take",
            effects=[Effect(kind="remove_item", target="iron_key", amount=1)],
            failure_events=["recovery"],
        ),
        _event(
            id="recovery",
            trigger_action="open",
            effects=[Effect(kind="set_flag", target="recovered", value=True)],
        ),
    ]
    state, executions, _ = run_triggered_events(world, state, "take", "iron_key", [])
    by_id = {e.event_id: e for e in executions}
    assert by_id["broken"].status == "failed"
    assert by_id["recovery"].status == "fired"
    assert state.flags == {"recovered": True}
    assert "broken" not in state.fired_events


def test_cooldown_blocks_until_enough_turns_have_passed():
    world, state = _setup()
    event = _event(
        id="pulse",
        trigger="state_change",
        repeat="always",
        cooldown_turns=2,
        effects=[Effect(kind="set_flag", target="pulse", value=True)],
    )
    world.events = [event]
    state, executions, _ = run_triggered_events(world, state, "look", None, [])
    assert [e.status for e in executions] == ["fired"]
    state.turn = 1
    state, executions, _ = run_triggered_events(world, state, "look", None, [])
    assert [e.status for e in executions] == ["skipped"]
    assert "cooldown" in executions[0].reason
    state.turn = 2
    state, executions, _ = run_triggered_events(world, state, "look", None, [])
    assert [e.status for e in executions] == ["fired"]
    assert state.event_counts["pulse"] == 2


def test_max_fires_is_a_hard_cap_and_is_recorded_in_simulation():
    world, state = _setup()
    event = _event(
        id="limited",
        trigger="state_change",
        repeat="always",
        max_fires=2,
        effects=[Effect(kind="set_flag", target="limited", value=True)],
    )
    world.events = [event]
    for turn in range(3):
        state.turn = turn
        state, executions, _ = run_triggered_events(world, state, "look", None, [])
    assert state.event_counts["limited"] == 2
    sim = simulate_event(world, state, "limited")
    assert sim["status"] == "skipped"
    assert "max_fires" in sim["reason"]


def test_followup_reference_is_validated():
    world, state = _setup()
    event = _event(id="caller", followup_events=["ghost"])
    world.events = [event]
    errors = validate_event(world, event)
    assert any("ghost" in error for error in errors)
    event.followup_events = ["caller"]
    assert any("itself" in error for error in validate_event(world, event))


# ------------------------------------------------------ conditional branches
def test_first_matching_branch_adds_effects_and_followups():
    world, state = _setup()
    state.flags["mood"] = "angry"
    world.events = [
        _event(
            id="reaction",
            trigger_action="look",
            effects=[Effect(kind="set_flag", target="base", value=True)],
            branches=[
                EventBranchDef(
                    label="calm",
                    conditions=[Condition(kind="flag", target="mood", value="calm")],
                    effects=[Effect(kind="set_flag", target="calm", value=True)],
                ),
                EventBranchDef(
                    label="angry",
                    conditions=[Condition(kind="flag", target="mood", value="angry")],
                    effects=[Effect(kind="set_flag", target="angry", value=True)],
                    followup_events=["aftermath"],
                ),
            ],
        ),
        _event(
            id="aftermath",
            trigger_action="open",
            effects=[Effect(kind="set_flag", target="aftermath", value=True)],
        ),
    ]
    state, executions, _ = run_triggered_events(world, state, "look", None, [])
    fired = {e.event_id: e for e in executions if e.status == "fired"}
    assert fired["reaction"].branch == "angry"
    assert state.flags == {"mood": "angry", "base": True, "angry": True, "aftermath": True}
    assert fired["aftermath"].branch == ""


def test_branch_effect_failure_is_atomic_and_runs_failure_followups():
    world, state = _setup()
    world.events = [
        _event(
            id="risky",
            trigger_action="look",
            effects=[Effect(kind="set_flag", target="base", value=True)],
            branches=[
                EventBranchDef(
                    label="bad",
                    conditions=[],
                    effects=[Effect(kind="remove_item", target="iron_key", amount=1)],
                )
            ],
            failure_events=["recover"],
        ),
        _event(
            id="recover",
            trigger_action="open",
            effects=[Effect(kind="set_flag", target="recovered", value=True)],
        ),
    ]
    state, executions, _ = run_triggered_events(world, state, "look", None, [])
    by_id = {e.event_id: e for e in executions}
    assert by_id["risky"].status == "failed"
    assert by_id["risky"].branch == "bad"
    assert by_id["recover"].status == "fired"
    assert state.flags == {"recovered": True}
    assert "base" not in state.flags


def test_unconditional_branch_must_be_last_and_is_validated():
    world, state = _setup()
    event = _event(
        id="branchy",
        branches=[
            EventBranchDef(label="fallback"),
            EventBranchDef(
                label="later",
                conditions=[Condition(kind="flag", target="x", value=True)],
            ),
        ],
    )
    world.events = [event]
    assert any("must be last" in error for error in validate_event(world, event))
    event.branches.reverse()
    assert validate_event(world, event) == []


def test_simulation_reports_the_matching_branch():
    world, state = _setup()
    state.flags["mood"] = "angry"
    event = _event(
        id="reaction",
        trigger="state_change",
        effects=[Effect(kind="set_flag", target="base", value=True)],
        branches=[
            EventBranchDef(
                label="angry",
                conditions=[Condition(kind="flag", target="mood", value="angry")],
                effects=[Effect(kind="set_flag", target="angry", value=True)],
            )
        ],
    )
    world.events = [event]
    sim = simulate_event(world, state, "reaction")
    assert sim["branch"] == "angry"
    assert {item["target"] for item in sim["effects_applied"]} == {"base", "angry"}


# ---------------------------------------------------- entity inventory
def test_entity_inventory_seeds_and_moves_both_directions():
    world, state = _setup()
    world.entities.append(
        EntityDef(
            id="chest", name="Chest", location_id="village_square",
            kind="object", items=["iron_key"],
        )
    )
    assert entity_item_count(state, world, "chest", "iron_key") == 1
    condition = Condition(kind="entity_item_count", target="chest", value="iron_key", op="gte", count=1)
    assert condition_holds(state, world, condition) is True

    taken = apply_effect(
        state, world,
        Effect(kind="take_entity", target="chest", value="iron_key", amount=1),
    )
    assert taken.ok and taken.facts[0].kind == "item_gained"
    assert state.inventory["iron_key"] == 1
    assert entity_item_count(state, world, "chest", "iron_key") == 0
    assert condition_holds(state, world, condition) is False

    returned = apply_effect(
        state, world,
        Effect(kind="give_entity", target="chest", value="iron_key", amount=1),
    )
    assert returned.ok
    assert "iron_key" not in state.inventory
    assert entity_item_count(state, world, "chest", "iron_key") == 1


def test_entity_inventory_effects_fail_closed():
    world, state = _setup()
    world.entities.append(
        EntityDef(id="chest", name="Chest", location_id="village_square", kind="object")
    )
    empty = apply_effect(
        state, world, Effect(kind="take_entity", target="chest", value="iron_key")
    )
    assert empty.ok is False and "holds 0" in empty.error
    state.item_locations.pop("iron_key", None)
    no_source = apply_effect(
        state, world, Effect(kind="give_entity", target="chest", value="iron_key")
    )
    assert no_source.ok is False and "not held" in no_source.error


def test_entity_item_count_requires_a_known_item():
    world, state = _setup()
    world.entities.append(
        EntityDef(id="chest", name="Chest", location_id="village_square", kind="object")
    )
    event = _event(
        id="loot_check",
        conditions=[Condition(kind="entity_item_count", target="chest", value="ghost_item")],
    )
    world.events = [event]
    errors = validate_event(world, event)
    assert any("ghost_item" in error for error in errors)
