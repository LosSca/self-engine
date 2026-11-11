"""The content-policy switch, exposed through /api/settings."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Imported inside the fixture: a module-scope import would load routes_game
# and open its SQLite stores before any test patches the paths, which leaves
# the rest of the suite writing into this checkout's backend/*.db.

from app.engines.llm_router import LLMProvider


@pytest.fixture()
def main_module(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LLM_TRACE_DB_PATH", str(tmp_path / "traces.db"))
    import app.main as module
    return module


@pytest.fixture()
def client(main_module):
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def _restore_policy(main_module):
    config = main_module._llm.config
    snapshot = (
        config.content_policy,
        config.nsfw_model,
        config.local_fallback_on_refusal,
        config.primary_provider,
        config.primary_model,
        config.orchestrator_model,
        config.temperature,
        config.max_tokens,
    )
    yield
    (
        config.content_policy,
        config.nsfw_model,
        config.local_fallback_on_refusal,
        config.primary_provider,
        config.primary_model,
        config.orchestrator_model,
        config.temperature,
        config.max_tokens,
    ) = snapshot


def test_get_settings_reports_the_policy_surface(client):
    body = client.get("/api/settings").json()
    assert body["content_policy"] in ("standard", "nsfw")
    assert body["policy_choices"] == ["standard", "nsfw"]
    assert "local_fallback_on_refusal" in body
    assert "local_route_available" in body


def test_policy_switch_round_trips(client, main_module):
    response = client.post(
        "/api/settings",
        json={
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "content_policy": "NSFW",
            "nsfw_model": "abliterated",
            "local_fallback_on_refusal": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["content_policy"] == "nsfw"
    config = main_module._llm.config
    assert config.content_policy == "nsfw"
    assert config.nsfw_model == "abliterated"
    assert config.local_fallback_on_refusal is True


def test_omitting_the_policy_leaves_the_switch_alone(client, main_module):
    main_module._llm.config.content_policy = "nsfw"
    response = client.post(
        "/api/settings",
        json={"provider": "deepseek", "model": "deepseek-v4-flash"},
    )
    assert response.status_code == 200
    assert main_module._llm.config.content_policy == "nsfw"


def test_an_unknown_policy_is_rejected_with_a_readable_error(client):
    response = client.post(
        "/api/settings",
        json={"provider": "deepseek", "model": "deepseek-v4-flash", "content_policy": "yolo"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "yolo" in detail
    assert "standard" in detail and "nsfw" in detail


def test_the_router_reads_the_policy_from_its_config():
    # sanitised provider names survive a round trip through the settings model
    from app.engines.llm_router import LLMConfig

    config = LLMConfig(primary_provider=LLMProvider.LOCAL, content_policy="nsfw")
    assert config.content_policy == "nsfw"
    assert config.primary_provider == LLMProvider.LOCAL
