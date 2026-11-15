"""端到端：说出口的话必须换来一段叙事，而不是一句拒绝。"""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import EntityDef
from app.services.text_action_service import TextActionService
from app.services.world_service import bind_world, save_world
from tests.support import village_world


@pytest.fixture
def scene(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world = village_world()
    # The fixture village has no entities; the square gets one who answers.
    world.entities.append(
        EntityDef(
            id="traveler",
            name="旅人",
            kind="npc",
            location_id="village_square",
            dialogue=["这里从前是观星台。"],
        )
    )
    save_world(store, world)
    bind_world(store, "camp-1", world.world_id, reset=True)
    yield TextActionService(store), store
    store.close()


async def test_saying_something_gets_a_narrative_answer(scene):
    service, store = scene
    result = await service.execute(
        "camp-1", "[SAY] 向旅人开口，问这里发生了什么", request_id="say-1", narrate=True
    )
    assert result["status"] != "rejected", result["result"].get("message")
    assert result["parsed"]["action"] == "talk"
    assert result["parsed"]["target_id"] == "traveler"
    narrative = result["result"]["narrative"]
    assert narrative["status"] == "ok"
    assert narrative["text"], "玩家说的话必须有叙事回应"
    assert "观星台" in narrative["text"] or "旅人" in narrative["text"]


async def test_saying_nothing_in_particular_still_reaches_the_person_here(scene):
    service, store = scene
    result = await service.execute("camp-1", "[SAY] 这里发生了什么？", request_id="say-2", narrate=True)
    assert result["status"] != "rejected"
    assert result["parsed"]["action"] == "talk"
    assert result["result"]["narrative"]["text"]

async def test_free_text_gets_a_narrative_and_changes_something(scene):
    """自由输入不能既没叙事、也没后果。

    我四处打听守卫的事 不是世界定义的动词，所以它变成一次尝试——但这次尝试
    必须是叙事能描述、世界能感觉到的。
    """
    service, store = scene
    before = store.get_state("camp-1").model_dump_json()
    result = await service.execute(
        "camp-1", "[DO] 我四处打听一下守卫的事", request_id="free-1", narrate=True
    )
    assert result["status"] == "applied", result
    assert result["parsed"]["action"] == "improvise"
    narrative = result["result"]["narrative"]
    assert narrative["status"] == "ok"
    assert narrative["text"].strip(), "自由输入也必须有叙事回应"
    after = store.get_state("camp-1").model_dump_json()
    assert after != before, "自由输入必须留下痕迹，否则等于没做"