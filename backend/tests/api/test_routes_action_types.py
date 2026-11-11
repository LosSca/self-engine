"""GET /api/v1/action-types exposes the catalogue the rules engine decides on."""
import os

from fastapi.testclient import TestClient

from app.engines.action_registry import action_names


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenario.db"))
    from app.main import app

    return TestClient(app)


def test_lists_every_registered_action(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/v1/action-types")
    assert response.status_code == 200
    payload = response.json()
    assert [item["name"] for item in payload] == list(action_names())
    look = next(item for item in payload if item["name"] == "look")
    assert look["target"] == "none"
    assert look["label"] == "观察"
    take = next(item for item in payload if item["name"] == "take")
    assert take["target"] == "required"
    assert "拿" in take["verbs"]
