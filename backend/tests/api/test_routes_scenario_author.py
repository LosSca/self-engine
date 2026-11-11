import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


def test_author_requires_a_model(client, monkeypatch):
    from app.services import ai_generator

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: None)
    res = client.post("/api/v1/scenarios/author", json={"prompt": "雾港"})
    assert res.status_code == 503
    assert "未配置可用模型" in res.json()["detail"]


def test_author_reports_a_draft_without_saving_when_asked(client, monkeypatch):
    from app.api import routes_engine
    from app.services import ai_generator

    seen = {}

    def fake_router_for(preset=None):
        return object()

    async def fake_author(router, prompt, world_id, scenario_id, language="中文", location_count=6):
        seen["scenario_id"] = scenario_id
        return {"world": None, "knowledge": [], "storyline": None, "warnings": ["只有一句梗概"]}

    monkeypatch.setattr(ai_generator, "router_for", fake_router_for)
    monkeypatch.setattr(
        routes_engine, "ai_generator", ai_generator, raising=False
    )
    from app.services import scenario_author

    monkeypatch.setattr(scenario_author, "author_scenario", fake_author)

    res = client.post(
        "/api/v1/scenarios/author",
        json={"prompt": "雾港", "save": False},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "draft"
    assert body["warnings"] == ["只有一句梗概"]
    # A dry run must not create a scenario row.
    assert body["scenario_id"] == ""
    assert seen["scenario_id"] == "draft"
    scenarios = client.get("/api/scenarios/").json()
    assert scenarios == []


def test_author_rejects_a_draft_that_produced_nothing(client, monkeypatch):
    from app.api import routes_engine
    from app.services import ai_generator, scenario_author

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: object())
    monkeypatch.setattr(routes_engine, "ai_generator", ai_generator, raising=False)

    async def boom(*args, **kwargs):
        raise ai_generator.GenerationError("模型返回的剧本没有任何可用部分")

    monkeypatch.setattr(scenario_author, "author_scenario", boom)
    res = client.post("/api/v1/scenarios/author", json={"prompt": "雾港"})
    assert res.status_code == 502
    assert "没有任何可用部分" in res.json()["detail"]
