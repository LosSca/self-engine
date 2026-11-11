"""GET /api/settings announces a custom endpoint without leaking its key."""

import os
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LLM_TRACE_DB_PATH", str(tmp_path / "traces.db"))
    from app.engines import llm_router

    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "https://vendor.example/v1")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_KEY", "sk-secret-key")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_MODEL", "vendor-model")
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_settings_report_the_custom_route_without_the_key(client):
    body = client.get("/api/settings").json()
    assert body["custom_route_available"] is True
    assert body["custom_model"] == "vendor-model"
    blob = str(body)
    assert "sk-secret-key" not in blob
    assert "vendor.example" not in blob


def test_a_missing_endpoint_leaves_the_route_off(client, monkeypatch):
    from app.engines import llm_router

    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_KEY", "")
    body = client.get("/api/settings").json()
    assert body["custom_route_available"] is False
    assert body["custom_model"] is None


def test_posting_a_model_switches_the_router_to_it(client):
    r = client.post("/api/settings", json={"provider": "openai", "model": "other-model"})
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "openai"
    assert body["model"] == "other-model"
    from app.api import routes_game

    assert routes_game._llm.config.orchestrator_model == "other-model"
