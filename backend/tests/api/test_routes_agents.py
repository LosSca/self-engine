"""Agent routing is inspectable over HTTP, without exposing credentials."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenario.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


def test_roles_are_listed_with_their_default_routing(client, monkeypatch):
    for name in ("AGENTS_SYSTEM_PROVIDER", "AGENTS_SYSTEM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    payload = client.get("/api/v1/agents").json()
    roles = {entry["role"]: entry for entry in payload["roles"]}
    assert set(roles) == {"narrative", "system", "memory"}
    assert roles["system"]["configured"] is False
    assert roles["system"]["model"] == ""
    assert payload["note"]


def test_a_configured_role_reports_its_model(client, monkeypatch):
    monkeypatch.setenv("AGENTS_SYSTEM_PROVIDER", "openai")
    monkeypatch.setenv("AGENTS_SYSTEM_MODEL", "sol-test")
    monkeypatch.setenv("AGENTS_SYSTEM_REASONING_EFFORT", "high")
    roles = {e["role"]: e for e in client.get("/api/v1/agents").json()["roles"]}
    assert roles["system"]["configured"] is True
    assert roles["system"]["model"] == "sol-test"
    assert roles["system"]["provider"] == "openai"
    assert roles["system"]["reasoning_effort"] == "high"


def test_no_credential_ever_appears_in_the_role_view(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    body = client.get("/api/v1/agents").text
    assert "sk-should-not-leak" not in body


def test_agent_calls_are_listed_per_campaign(client):
    from app.api import routes_engine

    store = routes_engine._get_store()
    try:
        store.record_agent_call(
            role="narrative", task="narrate", model="m", campaign_id="c-agent",
        )
    finally:
        store.close()
    rows = client.get("/api/v1/campaigns/c-agent/agent-calls").json()
    assert [row["role"] for row in rows] == ["narrative"]
    assert client.get("/api/v1/campaigns/other/agent-calls").json() == []


def test_agent_call_role_filter_is_validated(client):
    assert client.get("/api/v1/campaigns/c1/agent-calls?role=nope").status_code == 422


def test_agent_calls_are_read_only(client):
    from app.api import routes_engine

    store = routes_engine._get_store()
    try:
        before = len(store.list_agent_calls(""))
    finally:
        store.close()
    client.get("/api/v1/campaigns/c1/agent-calls")
    store = routes_engine._get_store()
    try:
        assert len(store.list_agent_calls("")) == before
    finally:
        store.close()