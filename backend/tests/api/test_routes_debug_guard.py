"""State-mutating debug endpoints must not be open on a production install."""
from fastapi.testclient import TestClient
import pytest

from tests.support import village_world_data


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    from app.main import app

    return TestClient(app)


def _bound_campaign(client, campaign_id="camp-1"):
    world = village_world_data()
    assert client.post("/api/v1/worlds", json=world).status_code == 200
    bound = client.post(
        f"/api/v1/campaigns/{campaign_id}/world",
        json={"world_id": world["world_id"], "reset": True},
    )
    assert bound.status_code == 200
    return campaign_id


def test_rollback_is_refused_when_debug_tools_are_off(client, monkeypatch):
    monkeypatch.setenv("ENGINE_DEBUG_TOOLS", "0")
    campaign = _bound_campaign(client)
    response = client.post(
        f"/api/v1/campaigns/{campaign}/rollback", json={"version": 0}
    )
    assert response.status_code == 403
    assert "ENGINE_DEBUG_TOOLS" in response.json()["detail"]


def test_repair_is_refused_when_debug_tools_are_off(client, monkeypatch):
    monkeypatch.setenv("ENGINE_DEBUG_TOOLS", "0")
    campaign = _bound_campaign(client)
    response = client.post(
        f"/api/v1/campaigns/{campaign}/repair", json={"dry_run": True}
    )
    assert response.status_code == 403


def test_reading_state_and_snapshots_stays_available(client, monkeypatch):
    monkeypatch.setenv("ENGINE_DEBUG_TOOLS", "0")
    campaign = _bound_campaign(client)
    assert client.get(f"/api/v1/campaigns/{campaign}/state").status_code == 200
    assert client.get(f"/api/v1/campaigns/{campaign}/diagnostics").status_code == 200
    assert client.get(f"/api/v1/campaigns/{campaign}/snapshots").status_code == 200


def test_the_switch_is_authoritative_when_set_to_on(client, monkeypatch):
    monkeypatch.setenv("ENGINE_DEBUG_TOOLS", "1")
    campaign = _bound_campaign(client)
    response = client.post(
        f"/api/v1/campaigns/{campaign}/repair", json={"dry_run": True}
    )
    assert response.status_code == 200


def test_without_the_switch_the_debug_flag_decides(monkeypatch):
    from app.api import routes_engine
    from app.config import settings

    monkeypatch.delenv("ENGINE_DEBUG_TOOLS", raising=False)
    monkeypatch.setattr(settings, "debug", False)
    assert routes_engine._debug_tools_enabled() is False
    monkeypatch.setattr(settings, "debug", True)
    assert routes_engine._debug_tools_enabled() is True
    monkeypatch.setenv("ENGINE_DEBUG_TOOLS", "0")
    assert routes_engine._debug_tools_enabled() is False
