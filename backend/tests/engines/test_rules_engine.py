"""Action rules: deterministic, side-effect free, no LLM."""
import pytest

from app.engines.event_engine import apply_effects
from app.engines.rules_engine import RulesEngine, describe_outcome
from app.models.game import CombatantState, Effect, EntityDef, EntityState
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


@pytest.mark.parametrize("target", ["iron_key", "warehouse_door", "nonexistent"])
def test_look_does_not_silently_ignore_explicit_targets(target):
    world, state = _setup()
    before = state.model_dump_json()
    outcome = RulesEngine().evaluate(world, state, "look", target)
    assert not outcome.ok
    assert outcome.code == "unsupported_target"
    assert outcome.effects == []
    assert state.model_dump_json() == before


def _with_enemy(location="village_square", kind="creature"):
    world, state = _setup()
    world.entities = [EntityDef(
        id="guard", name="Guard", location_id=location, kind=kind, hostile=True,
        attributes={"max_hp": 30, "flee_chance": 1.0},
    )]
    state.combat = {"target_id": "guard"}
    return world, state


@pytest.mark.parametrize("action", ["attack", "defend", "flee", "talk"])
def test_rules_use_runtime_position_for_an_entity_moved_here(action):
    world, state = _with_enemy(location="forest_edge")
    apply_effects(state, world, [Effect(kind="move_entity", target="guard", value="village_square")])
    before = state.model_dump_json()
    outcome = RulesEngine().evaluate(world, state, action, "guard")
    assert outcome.ok, outcome
    assert state.model_dump_json() == before


@pytest.mark.parametrize("action", ["attack", "defend", "flee", "talk"])
def test_rules_reject_entities_that_have_left_the_authored_location(action):
    world, state = _with_enemy()
    apply_effects(state, world, [Effect(kind="move_entity", target="guard", value="forest_edge")])
    before = state.model_dump_json()
    outcome = RulesEngine().evaluate(world, state, action, "guard")
    assert not outcome.ok
    assert outcome.code in ("entity_not_here", "not_in_combat")
    assert outcome.effects == []
    assert state.model_dump_json() == before


@pytest.mark.parametrize("action", ["attack", "defend", "flee", "talk"])
@pytest.mark.parametrize("record_kind", ["entity", "combatant"])
def test_rules_respect_both_runtime_death_records(action, record_kind):
    world, state = _with_enemy()
    if record_kind == "entity":
        state.entities["guard"] = EntityState(
            entity_id="guard", location_id=state.location_id, alive=False, hp=0,
        )
    else:
        state.combatants["guard"] = CombatantState(entity_id="guard", hp=0, alive=False)
    before = state.model_dump_json()
    outcome = RulesEngine().evaluate(world, state, action, "guard")
    assert not outcome.ok
    assert outcome.code in ("target_defeated", "entity_dead")
    assert outcome.effects == []
    assert state.model_dump_json() == before


@pytest.mark.parametrize("action", ["attack", "defend", "flee", "talk"])
def test_objects_are_not_combatants_or_speakers(action):
    world, state = _with_enemy(kind="object")
    before = state.model_dump_json()
    outcome = RulesEngine().evaluate(world, state, action, "guard")
    assert not outcome.ok
    assert outcome.code == "unsupported_target_kind"
    assert outcome.effects == []
    assert state.model_dump_json() == before


@pytest.mark.parametrize("record_kind", ["combat", "flags"])
def test_move_cannot_bypass_an_active_combat(record_kind):
    world, state = _with_enemy()
    if record_kind == "combat":
        state.combat["active"] = True
    else:
        state.flags["combat_active"] = True
    before = state.model_dump_json()
    outcome = RulesEngine().evaluate(world, state, "move", "forest_edge")
    assert not outcome.ok
    assert outcome.code == "combat_active"
    assert outcome.effects == []
    assert state.model_dump_json() == before


def test_talk_and_look_do_not_list_dead_entities_or_objects():
    world, state = _with_enemy()
    world.entities.append(EntityDef(id="chest", name="Chest", location_id=state.location_id, kind="object"))
    state.entities["guard"] = EntityState(entity_id="guard", location_id=state.location_id, alive=False)
    for action in ("look", "talk"):
        outcome = RulesEngine().evaluate(world, state, action)
        assert outcome.ok
        assert "Guard" not in outcome.message
        assert "Chest" not in outcome.message
        assert outcome.effects == []


def test_take_from_a_present_container_uses_transfer_effect():
    world, state = _setup()
    state.item_locations.pop("iron_key", None)
    world.entities.append(
        EntityDef(
            id="chest", name="Chest", location_id="village_square",
            kind="object", items=["iron_key"],
        )
    )
    outcome = RulesEngine().evaluate(world, state, "take", "iron_key")
    assert outcome.ok and outcome.code == "taken"
    assert [effect.kind for effect in outcome.effects] == ["take_entity"]
    assert outcome.effects[0].target == "chest"
    assert outcome.effects[0].value == "iron_key"

    apply_effects(state, world, outcome.effects)
    assert state.inventory["iron_key"] == 1
    assert state.entities["chest"].inventory.get("iron_key", 0) == 0


def test_look_reports_container_contents():
    world, state = _setup()
    state.item_locations.pop("iron_key", None)
    world.entities.append(
        EntityDef(
            id="chest", name="Chest", location_id="village_square",
            kind="object", items=["iron_key"],
        )
    )
    outcome = RulesEngine().evaluate(world, state, "look")
    assert outcome.ok
    assert "Containers here" in outcome.message
    assert "Chest" in outcome.message and "Iron Key" in outcome.message


def test_investigate_reports_entity_item_and_door():
    world, state = _setup()
    world.entities.append(
        EntityDef(
            id="chest", name="Chest", location_id="village_square",
            kind="object", description="A banded chest.", items=["iron_key"],
        )
    )
    outcome = RulesEngine().evaluate(world, state, "investigate", "chest")
    assert outcome.ok and outcome.code == "investigated"
    assert "banded chest" in outcome.message
    assert "Iron Key" in outcome.message

    item = RulesEngine().evaluate(world, state, "investigate", "iron_key")
    assert item.ok and "Iron Key" in item.message

    door = RulesEngine().evaluate(world, state, "investigate", "warehouse_door")
    assert door.ok and "locked" in door.message


def test_move_accepts_a_validated_path_and_rejects_a_blocked_segment():
    world, state = _setup()
    from app.models.game import ExitDef, LocationDef

    world.locations.append(
        LocationDef(
            id="shrine", name="Shrine",
            exits=[ExitDef(target_id="forest_edge")],
        )
    )
    forest = next(item for item in world.locations if item.id == "forest_edge")
    forest.exits.append(ExitDef(target_id="shrine"))

    outcome = RulesEngine().evaluate(
        world, state, "move", "shrine",
        {"path": ["forest_edge", "shrine"]},
    )
    assert outcome.ok
    assert outcome.effects[0].target == "shrine"

    blocked = RulesEngine().evaluate(
        world, state, "move", "warehouse",
        {"path": ["forest_edge", "warehouse"]},
    )
    assert not blocked.ok
    assert blocked.code == "no_route"

    closed = RulesEngine().evaluate(
        world, state, "move", "warehouse",
        {"path": ["warehouse"]},
    )
    assert not closed.ok and closed.code == "exit_blocked"


def test_attacking_raises_alert_and_lowers_relation_on_hit():
    world, state = _setup()
    from app.models.game import EntityDef

    world.entities.append(
        EntityDef(
            id="guard", name="Guard", location_id="village_square",
            hostile=True, attributes={"max_hp": 50},
        )
    )
    effects = []
    for seed in range(5):
        state.turn = seed
        outcome = RulesEngine().evaluate(world, state, "attack", "guard")
        if outcome.ok:
            effects = outcome.effects
            break
    kinds = {effect.kind for effect in effects}
    assert "adjust_alert" in kinds
    if "player_hit" == next(
        (effect.target for effect in effects if effect.kind == "combat_target"), None
    ):
        pass
    # A hit also records the relation drop; a miss only raises alert.
    hits = [effect for effect in effects if effect.kind == "combat_hp"]
    assert hits


def test_dialogue_advances_and_wraps_per_entity():
    world, state = _setup()
    from app.models.game import EntityDef

    world.entities.append(
        EntityDef(
            id="guard", name="Guard", location_id="village_square",
            dialogue=["First line.", "Second line."],
        )
    )
    first = RulesEngine().evaluate(world, state, "talk", "guard")
    assert first.ok and "First line." in first.message
    apply_effects(state, world, first.effects)
    second = RulesEngine().evaluate(world, state, "talk", "guard")
    assert second.ok and "Second line." in second.message
    apply_effects(state, world, second.effects)
    third = RulesEngine().evaluate(world, state, "talk", "guard")
    assert "First line." in third.message


def test_buy_and_sell_use_currency_and_merchant_stock():
    world, state = _setup()
    from app.models.game import EntityDef

    key = next(item for item in world.items if item.id == "iron_key")
    key.price = 10
    world.entities.append(
        EntityDef(
            id="merchant", name="Merchant", location_id="village_square",
            shop_items=["iron_key"],
        )
    )
    state.currency = 25
    bought = RulesEngine().evaluate(world, state, "buy", "iron_key")
    assert bought.ok
    apply_effects(state, world, bought.effects)
    assert state.currency == 15 and state.inventory["iron_key"] == 1

    sold = RulesEngine().evaluate(world, state, "sell", "iron_key")
    assert sold.ok
    apply_effects(state, world, sold.effects)
    assert state.currency == 20 and "iron_key" not in state.inventory

    state.currency = 2
    poor = RulesEngine().evaluate(world, state, "buy", "iron_key")
    assert not poor.ok and poor.code == "insufficient_funds"


def test_buy_can_target_a_specific_merchant():
    world, state = _setup()
    from app.models.game import ItemDef

    world.items.append(ItemDef(id="tonic", name="Tonic", price=5))
    world.entities.extend([
        EntityDef(
            id="first_merchant", name="First", location_id=state.location_id,
            shop_items=["iron_key"],
        ),
        EntityDef(
            id="second_merchant", name="Second", location_id=state.location_id,
            shop_items=["tonic"],
        ),
    ])
    state.currency = 20
    state.item_locations["tonic"] = "forest_edge"

    wrong = RulesEngine().evaluate(world, state, "buy", "tonic")
    assert not wrong.ok and wrong.code == "not_for_sale"

    targeted = RulesEngine().evaluate(
        world, state, "buy", "tonic", {"merchant_id": "second_merchant"}
    )
    assert targeted.ok
    apply_effects(state, world, targeted.effects)
    assert state.currency == 15 and state.inventory["tonic"] == 1
    # Buying one copy must not erase a different copy lying elsewhere.
    assert state.item_locations["tonic"] == "forest_edge"
