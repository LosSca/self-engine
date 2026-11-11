import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.db.game_state_store import GameStateStore
    from app.db.scenario_store import ScenarioStore

    game_path = tmp_path / "game_state.db"
    scenario_path = tmp_path / "scenarios.db"
    monkeypatch.setenv("SCENARIO_DB_PATH", str(scenario_path))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(game_path))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    with TestClient(app) as c:
        c.game_path = game_path
        c.scenario_path = scenario_path
        yield c


WORLD = {
    "name": "雾港",
    "description": "一座被雾笼罩的港口小城。",
    "start_location_id": "harbor",
    "locations": [
        {
            "id": "harbor",
            "name": "港口",
            "description": "栈桥尽头有一盏灯。",
            "region": "海岸",
            "door_ids": ["warehouse_door"],
            "item_ids": ["brass_key"],
            "npc_ids": ["keeper"],
            "exits": [{"target_id": "warehouse", "label": "走向仓库", "door_id": "warehouse_door"}],
        },
        {
            "id": "warehouse",
            "name": "仓库",
            "description": "货箱堆到屋顶。",
            "region": "海岸",
            "exits": [{"target_id": "harbor", "label": "回到港口"}],
        },
    ],
    "items": [
        {"id": "brass_key", "name": "铜钥匙", "description": "刻着货号。", "tags": ["key"]}
    ],
    "doors": [
        {
            "id": "warehouse_door",
            "name": "仓库铁门",
            "location_id": "harbor",
            "locked": True,
            "key_item_id": "brass_key",
        }
    ],
    "entities": [
        {"id": "keeper", "name": "守夜人", "description": "他不看海。", "location_id": "harbor", "kind": "npc"}
    ],
    "quests": [],
    "events": [],
}

DRAFT = {"world": WORLD, "knowledge": [], "storyline": None, "warnings": ["第 2 条知识未通过校验"]}


def test_author_saves_the_draft_into_the_chosen_scenario(client, monkeypatch):
    from app.api import routes_engine
    from app.db.game_state_store import GameStateStore
    from app.db.scenario_store import ScenarioStore
    from app.services import ai_generator, scenario_author, world_service

    scenario_store = ScenarioStore(str(client.scenario_path))
    scenario = scenario_store.create_scenario(title="雾港剧本")
    scenario_store.close()

    game = GameStateStore(str(client.game_path))
    world = world_service.WorldDefinition.model_validate(
        dict(WORLD, world_id="ai_1", scenario_id=scenario.id)
    )
    package = {
        "world": world,
        "knowledge": [],
        "storyline": None,
        "warnings": ["第 2 条知识未通过校验"],
    }

    async def fake_author(router, prompt, world_id, scenario_id, language="中文", location_count=6):
        assert scenario_id == scenario.id
        return package

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: object())
    monkeypatch.setattr(routes_engine, "ai_generator", ai_generator, raising=False)
    monkeypatch.setattr(scenario_author, "author_scenario", fake_author)

    res = client.post(
        "/api/v1/scenarios/author",
        json={"prompt": "雾中的港口", "scenario_id": scenario.id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "saved"
    assert body["world_saved"] is True
    assert body["counts"]["locations"] == 2
    assert body["warnings"] == ["第 2 条知识未通过校验"]

    # The world really is readable from the store the API wrote to.
    game.close()
    stored = GameStateStore(str(client.game_path))
    try:
        loaded = stored.get_world("ai_1")
        assert loaded is not None
        assert loaded.scenario_id == scenario.id
    finally:
        stored.close()


def test_author_rejects_an_unknown_scenario(client, monkeypatch):
    from app.services import ai_generator

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: object())
    res = client.post(
        "/api/v1/scenarios/author",
        json={"prompt": "雾中的港口", "scenario_id": "nope"},
    )
    assert res.status_code == 404
