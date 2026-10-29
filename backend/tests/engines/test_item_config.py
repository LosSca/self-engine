"""Configurable items: pickup gating and use effects are validated data.

A world can say "the charm cannot be lifted until the statue is toppled" and
"drinking the potion sets 'healed' and spends it" without a single line of code
being executed. These tests pin the authoring-time validation and the runtime
behaviour of both hooks, plus the built-in fallback (a key standing at its door).
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import ActionRequest, Condition, Effect, ItemDef, WorldDefinition
from app.services.action_service import ActionService
from app.services.world_service import WorldValidationError, bind_world, save_world
from tests.support import village_world


def _world_with(*, items=None, where: dict | None = None) -> WorldDefinition:
    """The demo village, optionally with extra items and item placement."""
    world = village_world()
    if items:
        by_id = {item.id: item for item in world.items}
        for item in items:
            by_id[item.id] = item
        world.items = list(by_id.values())
    for location_id, item_ids in (where or {}).items():
        location = next(loc for loc in world.locations if loc.id == location_id)
        location.item_ids = list(location.item_ids) + list(item_ids)
    assert world.validate_references() == [], world.validate_references()
    return world


def _gated_coin() -> ItemDef:
    return ItemDef(
        id="lucky_coin",
        name="Lucky Coin",
        pickup_requires=[Condition(kind="flag", target="statue_toppled", value=True)],
    )


def _potion() -> ItemDef:
    return ItemDef(
        id="potion",
        name="Healing Potion",
        use_effects=[Effect(kind="set_flag", target="healed", value=True)],
        consumable=True,
    )


def _request(action: str, target_id: str, request_id: str) -> ActionRequest:
    return ActionRequest(
        campaign_id="camp-1",
        request_id=request_id,
        action=action,
        target_id=target_id,
        narrate=False,
    )


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def _bind(store, world: WorldDefinition):
    save_world(store, world)
    return bind_world(store, "camp-1", world.world_id, reset=True)


def _commit(store, state) -> None:
    """Persist a hand-edited state the way a previous turn would have left it."""
    store.commit_action(
        campaign_id=state.campaign_id,
        request_id=f"seed-{state.version}",
        request_payload={},
        status="applied",
        result_payload={},
        state=state,
        events=[],
        expected_version=None,
    )


# ------------------------------------------------------------- authoring time
def test_an_item_may_declare_pickup_conditions_and_use_effects():
    world = _world_with(items=[_gated_coin(), _potion()])
    charm = next(item for item in world.items if item.id == "lucky_coin")
    assert charm.pickup_requires[0].target == "statue_toppled"
    potion = next(item for item in world.items if item.id == "potion")
    assert potion.use_effects[0].kind == "set_flag"
    assert potion.consumable is True


def test_validation_rejects_a_pickup_condition_on_an_unknown_item():
    world = village_world()
    world.items.append(
        ItemDef(
            id="cursed_ring",
            name="Cursed Ring",
            pickup_requires=[Condition(kind="has_item", target="ghost_item")],
        )
    )
    errors = world.validate_references()
    assert any(
        "item 'cursed_ring' pickup condition: unknown item 'ghost_item'" in e
        for e in errors
    ), errors


def test_validation_rejects_a_use_effect_on_an_unknown_door():
    world = village_world()
    world.items.append(
        ItemDef(
            id="odd_lantern",
            name="Odd Lantern",
            use_effects=[Effect(kind="open_door", target="ghost_door")],
        )
    )
    errors = world.validate_references()
    assert any(
        "item 'odd_lantern' use effect: unknown door 'ghost_door'" in e for e in errors
    ), errors


def test_validation_rejects_an_unknown_use_effect_kind():
    with pytest.raises(Exception) as excinfo:
        ItemDef(
            id="bomb",
            name="Bomb",
            use_effects=[{"kind": "run_python", "target": "boom"}],
        )
    assert "run_python" in str(excinfo.value)


def test_the_world_editor_refuses_an_invalid_item(store):
    from app.services import world_editor

    world = village_world()
    save_world(store, world)
    with pytest.raises(WorldValidationError) as excinfo:
        world_editor.upsert_item(
            world,
            ItemDef(
                id="odd_lantern",
                name="Odd Lantern",
                use_effects=[Effect(kind="give_item", target="ghost_gift")],
            ),
            "odd_lantern",
        )
    assert "unknown item 'ghost_gift'" in str(excinfo.value)
    assert all(item.id != "odd_lantern" for item in store.get_world(world.world_id).items)


# ---------------------------------------------------------------- rule engine
async def test_pickup_is_refused_while_a_condition_is_unmet(store):
    world = _world_with(items=[_gated_coin()], where={"village_square": ["lucky_coin"]})
    _bind(store, world)
    service = ActionService(store)

    result = await service.execute(_request("take", "lucky_coin", "req-charm-1"))

    assert result.status == "rejected"
    assert result.code == "pickup_refused"
    assert "statue_toppled" in result.message
    assert store.get_state("camp-1").inventory.get("lucky_coin") is None


async def test_pickup_succeeds_once_the_condition_holds(store):
    world = _world_with(items=[_gated_coin()], where={"village_square": ["lucky_coin"]})
    state = _bind(store, world)
    state.flags["statue_toppled"] = True
    _commit(store, state)
    service = ActionService(store)

    result = await service.execute(_request("take", "lucky_coin", "req-charm-2"))

    assert result.status == "applied"
    assert result.code == "taken"
    assert store.get_state("camp-1").inventory["lucky_coin"] == 1


async def test_using_an_item_applies_its_effects_and_spends_it(store):
    world = _world_with(items=[_potion()], where={"village_square": ["potion"]})
    state = _bind(store, world)
    state.inventory["potion"] = 1
    _commit(store, state)
    service = ActionService(store)

    result = await service.execute(_request("use", "potion", "req-potion-1"))

    assert result.status == "applied"
    assert result.code == "used_item"
    after = store.get_state("camp-1")
    assert after.flags.get("healed") is True
    assert after.inventory.get("potion") is None


async def test_a_non_consumable_item_survives_being_used(store):
    world = _world_with(
        items=[
            ItemDef(
                id="silver_charm",
                name="Silver Charm",
                use_effects=[Effect(kind="set_flag", target="charm_rubbed", value=True)],
                consumable=False,
            )
        ]
    )
    state = _bind(store, world)
    state.inventory["silver_charm"] = 1
    _commit(store, state)
    service = ActionService(store)

    result = await service.execute(_request("use", "silver_charm", "req-charm-3"))

    assert result.status == "applied", result.message
    after = store.get_state("camp-1")
    assert after.flags.get("charm_rubbed") is True
    assert after.inventory["silver_charm"] == 1


async def test_an_item_without_use_effects_keeps_the_built_in_key_behaviour(store):
    world = _world_with()
    _bind(store, world)
    service = ActionService(store)

    taken = await service.execute(_request("take", "iron_key", "req-key-1"))
    assert taken.status == "applied", taken.message
    opened = await service.execute(_request("use", "iron_key", "req-key-2"))

    assert opened.status == "applied"
    assert opened.code == "used_item"
    assert store.get_state("camp-1").doors["warehouse_door"] is True


async def test_a_refused_pickup_does_not_consume_the_request_id(store):
    world = _world_with(items=[_gated_coin()], where={"village_square": ["lucky_coin"]})
    _bind(store, world)
    service = ActionService(store)

    first = await service.execute(_request("take", "lucky_coin", "req-charm-4"))
    again = await service.execute(_request("take", "lucky_coin", "req-charm-4"))

    assert first.status == "rejected" and first.code == "pickup_refused"
    assert again.status == "duplicate"
    assert again.code == "pickup_refused"
    assert store.get_state("camp-1").inventory.get("lucky_coin") is None
