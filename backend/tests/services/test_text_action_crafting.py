"""Typed free-form text reaches the crafting verbs.

The parser is the only place that reads prose. Everything downstream - target
resolution, preconditions, effects - stays in the rule engine, so these tests
also assert that the parser never invents a target that is not carried.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import ItemDef
from app.services.text_action_service import TextActionService
from tests.support import village_world
from app.services.world_service import bind_world, save_world


@pytest.fixture
def campaign(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world = village_world()
    world = world.model_copy(
        update={
            "items": list(world.items)
            + [
                ItemDef(id="wooden_table", name="木桌", dismantles_to=["plank", "plank"]),
                ItemDef(id="plank", name="木板"),
                ItemDef(
                    id="wooden_shield",
                    name="木盾",
                    craftable=True,
                    made_from=["plank", "plank"],
                ),
            ]
        }
    )
    save_world(store, world)
    state = bind_world(store, "camp-1", world.world_id, reset=True)
    store.bind_campaign(state.model_copy(update={"inventory": {"wooden_table": 1}}))
    service = TextActionService(store)
    yield service, store, world
    store.close()


async def test_chinese_dismantle_utterance_takes_the_table_apart(campaign):
    service, _store, _world = campaign
    result = await service.execute("camp-1", "我拆掉木桌", request_id="t1", narrate=False)
    payload = result["result"]
    assert payload["ok"] is True, payload
    assert payload["code"] == "dismantled"
    assert payload["state"]["inventory"].get("plank") == 2


async def test_table_that_is_not_carried_is_not_offered_to_the_parser(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world = village_world()
    world = world.model_copy(
        update={
            "items": list(world.items)
            + [
                ItemDef(id="wooden_table", name="木桌", dismantles_to=["plank"]),
                ItemDef(id="plank", name="木板"),
            ]
        }
    )
    save_world(store, world)
    bind_world(store, "camp-2", world.world_id, reset=True)
    service = TextActionService(store)

    result = await service.execute("camp-2", "拆掉木桌", request_id="t1", narrate=False)
    # The parser cannot resolve a target it is not holding, so the request is
    # rejected before any rule runs - and nothing was dismantled.
    assert result["result"]["ok"] is False
    assert store.get_state("camp-2").inventory.get("plank", 0) == 0
    store.close()


def _parse(service, world, campaign_id="camp-1", text=""):
    """Parse exactly the way `execute` does: the campaign's own world + state."""
    state = service.ensure_state(campaign_id)
    return service.parser.parse(text, world, state)


def test_wait_reads_a_spoken_duration(campaign):
    service, _store, world = campaign
    parsed = _parse(service, world, text="等待半小时")
    assert parsed.action == "wait"
    assert parsed.params["minutes"] == 30


def test_wait_without_a_duration_uses_the_catalogue_default(campaign):
    service, _store, world = campaign
    parsed = _parse(service, world, text="等一下")
    assert parsed.action == "wait"
    # No duration in the utterance: the rule falls back to the catalogue's 60.
    assert "minutes" not in parsed.params


async def test_combine_lists_a_missing_part(campaign):
    service, _store, _world = campaign
    result = await service.execute("camp-1", "合成木盾", request_id="t2", narrate=False)
    payload = result["result"]
    assert payload["ok"] is False
    assert payload["code"] == "missing_parts"
    assert "木板" in payload["message"]
