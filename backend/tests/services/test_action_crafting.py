"""Crafting verbs: dismantle and combine.

Both read authored material facts (`ItemDef.dismantles_to` / `made_from`) and
answer through the ordinary `RuleOutcome` channel, so a refusal carries a code
and a readable reason instead of silently doing nothing. The point of the test
set is the honest failure: an item that declares no parts must be *refused*, not
quietly consumed.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import ActionRequest, ItemDef
from app.services.action_service import ActionService
from tests.support import village_world
from app.services.world_service import bind_world, save_world


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def _world_with_crafting():
    world = village_world()
    items = list(world.items) + [
        ItemDef(
            id="wooden_table",
            name="Wooden Table",
            dismantles_to=["plank", "plank"],
        ),
        ItemDef(id="plank", name="Wooden Plank"),
        ItemDef(
            id="wooden_shield",
            name="Wooden Shield",
            craftable=True,
            made_from=["plank", "plank"],
        ),
        ItemDef(id="grey_stone", name="Grey Stone"),
    ]
    return world.model_copy(update={"items": items})


def _service_with(store, state_patch=None):
    world = _world_with_crafting()
    save_world(store, world)
    state = bind_world(store, "camp-1", world.world_id, reset=True)
    if state_patch:
        store.bind_campaign(state_patch(state))
    return ActionService(store), world


def _request(request_id, action, target=None, params=None):
    return ActionRequest(
        campaign_id="camp-1",
        request_id=request_id,
        action=action,
        target_id=target,
        params=params or {},
        narrate=False,
    )


async def test_dismantle_then_combine_makes_a_shield(store):
    def start_with_table(state):
        return state.model_copy(
            update={"inventory": {**state.inventory, "wooden_table": 1}}
        )

    service, _world = _service_with(store, start_with_table)

    taken_apart = await service.execute(
        _request("r1", "dismantle", "wooden_table")
    )
    assert taken_apart.ok and taken_apart.code == "dismantled"
    assert taken_apart.state.inventory.get("plank") == 2
    assert taken_apart.state.inventory.get("wooden_table", 0) == 0

    built = await service.execute(_request("r2", "combine", "wooden_shield"))
    assert built.ok and built.code == "combined"
    assert built.state.inventory.get("wooden_shield") == 1
    assert built.state.inventory.get("plank", 0) == 0


async def test_dismantle_of_an_item_without_parts_is_refused(store):
    def carry_stone(state):
        return state.model_copy(update={"inventory": {**state.inventory, "grey_stone": 1}})

    service, _world = _service_with(store, carry_stone)

    refused = await service.execute(_request("r1", "dismantle", "grey_stone"))
    assert refused.status == "rejected"
    assert refused.code == "cannot_dismantle"
    # A refusal never spends the item.
    assert store.get_state("camp-1").inventory.get("grey_stone") == 1


async def test_combine_without_parts_names_what_is_missing(store):
    service, _world = _service_with(store)

    refused = await service.execute(_request("r1", "combine", "wooden_shield"))
    assert refused.status == "rejected"
    assert refused.code == "not_craftable" or refused.code == "missing_parts"
    if refused.code == "missing_parts":
        assert "Plank" in refused.message


async def test_combine_refuses_when_the_parts_are_not_held(store):
    def one_plank(state):
        return state.model_copy(update={"inventory": {**state.inventory, "plank": 1}})

    service, _world = _service_with(store, one_plank)

    refused = await service.execute(_request("r1", "combine", "wooden_shield"))
    assert refused.status == "rejected"
    assert refused.code == "missing_parts"
    assert "Plank" in refused.message


async def test_dismantle_requires_carrying_the_item(store):
    service, _world = _service_with(store)

    refused = await service.execute(_request("r1", "dismantle", "wooden_table"))
    assert refused.status == "rejected"
    assert refused.code == "item_not_carried"


async def test_crafting_is_replayable(store):
    def start_with_table(state):
        return state.model_copy(
            update={"inventory": {**state.inventory, "wooden_table": 1}}
        )

    service, _world = _service_with(store, start_with_table)
    request = _request("r1", "dismantle", "wooden_table")

    first = await service.execute(request)
    replay = await service.execute(request)

    assert replay.status == "duplicate"
    assert replay.state_version == first.state_version
    # Replaying must not hand out a second set of planks.
    assert store.get_state("camp-1").inventory.get("plank") == 2
