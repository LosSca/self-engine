"""Action rules: deterministic, side-effect free, no LLM."""
from app.engines.rules_engine import RulesEngine, describe_outcome
from app.services.world_service import build_initial_state
from tests.support import village_world


def _setup():
    world = village_world()
    return world, build_initial_state(world, "camp-1")


def _take_key(world, state):
    state.inventory["iron_key"] = 1
    state.item_locations.pop("iron_key", None)
    return state


def test_look_lists_visible_items_and_exits():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "look")
    assert outcome.ok
    assert outcome.code == "looked"
    assert "Iron Key" in outcome.message
    assert "Exits" in outcome.message


def test_move_to_reachable_location():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "move", "forest_edge")
    assert outcome.ok
    assert outcome.code == "moved"
    assert [effect.kind for effect in outcome.effects] == ["move_player"]
    assert outcome.effects[0].target == "forest_edge"


def test_move_through_closed_door_is_rejected():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "move", "warehouse")
    assert not outcome.ok
    assert outcome.code == "exit_blocked"
    assert "closed" in outcome.message


def test_move_to_unknown_location_is_rejected():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "move", "moon")
    assert not outcome.ok
    assert outcome.code == "unknown_location"


def test_take_item_in_current_location():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "take", "iron_key")
    assert outcome.ok
    assert outcome.code == "taken"
    assert outcome.effects[0].kind == "give_item"
    assert outcome.effects[0].target == "iron_key"


def test_take_item_elsewhere_is_rejected():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "take", "silver_charm")
    assert not outcome.ok
    assert outcome.code == "item_not_here"


def test_take_unknown_item_is_rejected():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "take", "ghost_item")
    assert not outcome.ok
    assert outcome.code == "unknown_item"


def test_open_door_without_key_is_rejected_with_readable_message():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "open", "warehouse_door")
    assert not outcome.ok
    assert outcome.code == "door_locked"
    assert "Iron Key" in outcome.message


def test_open_door_with_key_unlocks_it():
    world, state = _setup()
    _take_key(world, state)
    outcome = RulesEngine().evaluate(world, state, "open", "warehouse_door")
    assert outcome.ok
    assert outcome.code == "door_unlocked"
    assert outcome.effects[0].kind == "open_door"
    assert outcome.effects[0].target == "warehouse_door"
    assert all(effect.kind != "remove_item" for effect in outcome.effects)


def test_open_door_consuming_key_also_removes_it():
    world, state = _setup()
    _take_key(world, state)
    door = next(d for d in world.doors if d.id == "warehouse_door")
    door.consumes_key = True
    outcome = RulesEngine().evaluate(world, state, "open", "warehouse_door")
    assert [effect.kind for effect in outcome.effects] == ["open_door", "remove_item"]


def test_open_already_open_door_is_rejected():
    world, state = _setup()
    state.doors["warehouse_door"] = True
    outcome = RulesEngine().evaluate(world, state, "open", "warehouse_door")
    assert not outcome.ok
    assert outcome.code == "door_already_open"


def test_open_door_from_another_location_is_rejected():
    world, state = _setup()
    state.location_id = "forest_edge"
    outcome = RulesEngine().evaluate(world, state, "open", "warehouse_door")
    assert not outcome.ok
    assert outcome.code == "door_not_here"


def test_evaluate_never_mutates_state():
    world, state = _setup()
    before = state.model_dump_json()
    engine = RulesEngine()
    engine.evaluate(world, state, "take", "iron_key")
    engine.evaluate(world, state, "open", "warehouse_door")
    engine.evaluate(world, state, "move", "forest_edge")
    assert state.model_dump_json() == before


def test_describe_outcome_is_readable():
    world, state = _setup()
    outcome = RulesEngine().evaluate(world, state, "open", "warehouse_door")
    text = describe_outcome(outcome)
    assert text.startswith("rejected:door_locked")
    assert "Iron Key" in text
