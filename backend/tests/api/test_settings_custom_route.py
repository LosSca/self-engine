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


def test_a_keyless_endpoint_saved_from_the_panel_is_reported_as_usable(
    client, monkeypatch, tmp_path
):
    """Filling in only an address must not look like an unsaved provider."""
    from app.db import credential_store
    from app.engines import llm_router

    for name in ("CUSTOM_LLM_BASE_URL", "CUSTOM_LLM_API_KEY", "CUSTOM_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_KEY", "")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_MODEL", "")
    monkeypatch.setenv("PREFS_DB_PATH", str(tmp_path / "prefs.db"))

    saved = client.post(
        "/api/providers/custom",
        json={"api_key": "", "base_url": "http://127.0.0.1:11434/v1", "model": "llama3"},
    )
    assert saved.status_code == 200
    rows = {row["id"]: row for row in saved.json()["providers"]}
    assert rows["custom"]["configured"] is True
    assert rows["custom"]["model"] == "llama3"
    assert rows["custom"]["keyless"] is True

    settings = client.get("/api/settings").json()
    assert settings["custom_route_available"] is True
    assert settings["custom_model"] == "llama3"


def test_a_stored_model_name_survives_a_save_without_one(client, tmp_path, monkeypatch):
    """A second save that only changes the key must not forget the model."""
    from app.db.credential_store import CredentialStore

    monkeypatch.setenv("PREFS_DB_PATH", str(tmp_path / "prefs.db"))
    client.post(
        "/api/providers/custom",
        json={"api_key": "sk-1", "base_url": "https://vendor.example/v1", "model": "m-1"},
    )
    client.post("/api/providers/custom", json={"api_key": "sk-2"})

    store = CredentialStore()
    try:
        row = store.rows()["custom"]
    finally:
        store.close()
    assert row["model"] == "m-1"
    assert row["api_key"] == "sk-2"


def test_clearing_a_stored_provider_restores_the_startup_value(client, tmp_path, monkeypatch):
    """The base URL must not outlive a delete either."""
    from app.engines import llm_router

    monkeypatch.setenv("PREFS_DB_PATH", str(tmp_path / "prefs.db"))
    monkeypatch.setenv("CUSTOM_LLM_BASE_URL", "https://startup.example/v1")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "https://startup.example/v1")
    client.post(
        "/api/providers/custom",
        json={"api_key": "sk-1", "base_url": "https://ui.example/v1", "model": "m-1"},
    )
    assert llm_router.custom_route_available() is True

    client.delete("/api/providers/custom")

    assert llm_router._custom_llm_url() == "https://startup.example/v1"


def test_testing_a_custom_provider_does_not_ask_for_a_provider_named_custom(
    client, tmp_path, monkeypatch
):
    """"custom" is a panel entry; the router only knows real providers."""
    from app.engines import llm_router

    monkeypatch.setenv("PREFS_DB_PATH", str(tmp_path / "prefs.db"))
    client.post(
        "/api/providers/custom",
        json={"api_key": "sk-1", "base_url": "https://vendor.example/v1", "model": "m-1"},
    )
    seen = {}

    async def fake_complete(self, messages, **kwargs):
        seen["model"] = self._build_model_string(
            self.config.primary_provider, self.config.primary_model
        )
        seen["api_base"] = self._get_api_base(self.config.primary_provider)
        seen["max_tokens"] = kwargs.get("max_tokens")
        return "ok"

    monkeypatch.setattr(llm_router.LLMRouter, "complete", fake_complete)
    body = client.post("/api/providers/custom/test", json={"model": "m-1"}).json()

    assert body["status"] == "ok", body
    assert body["reply"] == "ok"
    assert seen["model"] == "openai/m-1"
    assert seen["api_base"] == "https://vendor.example/v1"
    # A reasoning model burns the probe budget thinking; 16 left it wordless.
    assert seen["max_tokens"] == 512


def test_a_blank_probe_reply_is_reported_as_a_problem(client, tmp_path, monkeypatch):
    from app.engines import llm_router

    monkeypatch.setenv("PREFS_DB_PATH", str(tmp_path / "prefs.db"))
    client.post("/api/providers/deepseek", json={"api_key": "sk-1"})

    async def empty(self, messages, **kwargs):
        return ""

    monkeypatch.setattr(llm_router.LLMRouter, "complete", empty)
    body = client.post("/api/providers/deepseek/test", json={}).json()

    assert body["status"] == "error"
    assert "空" in body["error"]
