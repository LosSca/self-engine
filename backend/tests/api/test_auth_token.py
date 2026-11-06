"""Optional bearer-token gate for the HTTP API."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    from app.main import app

    return TestClient(app)


def test_api_token_is_optional(client, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    assert client.get("/api/scenarios/").status_code == 200


def test_cors_preflight_is_not_treated_as_an_authenticated_request(
    client, monkeypatch
):
    monkeypatch.setenv("API_TOKEN", "s3cret-token")
    res = client.options(
        "/api/scenarios/",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert res.status_code != 401


def test_api_token_guards_routes_but_not_health(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret-token")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/scenarios/").status_code == 401
    assert client.get("/api/scenarios/", headers={"X-API-Token": "wrong"}).status_code == 401
    assert (
        client.get(
            "/api/scenarios/", headers={"Authorization": "Bearer s3cret-token"}
        ).status_code
        == 200
    )
    assert (
        client.get("/api/scenarios/", headers={"X-API-Token": "s3cret-token"}).status_code
        == 200
    )
