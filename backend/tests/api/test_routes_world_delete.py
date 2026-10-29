"""Deleting a world keeps campaign history inspectable."""
import pytest
from fastapi.testclient import TestClient

from tests.support import village_world_data

API = "/api/v1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    from app.main import app

    return TestClient(app)


def _bound(client):
    assert client.post(f"{API}/worlds", json=village_world_data()).status_code == 200
    assert client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"}).status_code == 200


def test_delete_world_removes_definition_only(client):
    _bound(client)
    assert client.get(f"{API}/worlds/demo_village").status_code == 200

    deleted = client.delete(f"{API}/worlds/demo_village")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": "demo_village"}

    assert client.get(f"{API}/worlds/demo_village").status_code == 404
    assert client.delete(f"{API}/worlds/demo_village").status_code == 404

    # The campaign's committed state survives, so history is still inspectable.
    state = client.get(f"{API}/campaigns/camp-1/state")
    assert state.status_code == 200, state.text
    assert state.json()["world_id"] == "demo_village"


def test_delete_world_keeps_snapshots_readable(client):
    _bound(client)
    assert client.post(
        f"{API}/campaigns/camp-1/actions",
        json={"campaign_id": "camp-1", "request_id": "req-look", "action": "look"},
    ).status_code == 200

    assert client.delete(f"{API}/worlds/demo_village").status_code == 200
    snapshots = client.get(f"{API}/campaigns/camp-1/snapshots")
    assert snapshots.status_code == 200, snapshots.text
    payload = snapshots.json()
    rows = payload["snapshots"] if isinstance(payload, dict) else payload
    versions = [item["version"] for item in rows]
    assert 1 in versions
