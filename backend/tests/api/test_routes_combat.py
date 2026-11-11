"""HTTP surface for deterministic combat."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


WORLD = {
    "world_id": "arena",
    "name": "Arena",
    "start_location_id": "square",
    "locations": [
        {
            "id": "square",
            "name": "Square",
            "description": "A dusty square.",
            "exits": [],
            "item_ids": [],
            "door_ids": [],
            "npc_ids": [],
        }
    ],
    "items": [],
    "doors": [],
    "entities": [
        {
            "id": "goblin",
            "name": "Goblin",
            "location_id": "square",
            "kind": "creature",
            "hostile": True,
            "attributes": {"max_hp": 6, "attack": 2, "defense": 1, "flee_chance": 1.0},
        }
    ],
    "quests": [],
    "events": [],
}


def _bind(client):
    assert client.post("/api/v1/worlds", json=WORLD).status_code in (200, 201)
    bound = client.post("/api/v1/campaigns/camp-1/world", json={"world_id": "arena"})
    assert bound.status_code == 200, bound.text


def test_combat_endpoint_updates_state_and_returns_round(client):
    _bind(client)
    response = client.post(
        "/api/v1/campaigns/camp-1/combat",
        json={"action": "attack", "target_id": "goblin", "request_id": "req-1", "narrate": False},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    assert body["combat_round"]["target_id"] == "goblin"
    assert body["state_version"] == 1

    state = client.get("/api/v1/campaigns/camp-1/state").json()
    assert "goblin" in state["combatants"]


def test_combat_endpoint_is_idempotent(client):
    _bind(client)
    payload = {"action": "attack", "target_id": "goblin", "request_id": "req-dup", "narrate": False}
    first = client.post("/api/v1/campaigns/camp-1/combat", json=payload).json()
    second = client.post("/api/v1/campaigns/camp-1/combat", json=payload).json()
    assert second["status"] == "duplicate"
    assert second["combat_round"] == first["combat_round"]
    assert client.get("/api/v1/campaigns/camp-1/state").json()["version"] == 1


def test_combat_rejects_unknown_target(client):
    _bind(client)
    response = client.post(
        "/api/v1/campaigns/camp-1/combat",
        json={"action": "attack", "target_id": "dragon", "request_id": "req-x"},
    )
    body = response.json()
    assert body["status"] == "rejected"
    assert body["code"] == "unknown_target"
