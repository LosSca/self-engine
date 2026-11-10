"""Action catalogue and dry-run preview: decide without writing."""
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
    "world_id": "preview_world",
    "name": "Preview",
    "start_location_id": "square",
    "locations": [
        {
            "id": "square",
            "name": "Square",
            "description": "A dusty square.",
            "exits": [{"target_id": "yard", "label": "Yard", "door_id": "gate"}],
            "item_ids": ["gem", "key"],
            "door_ids": ["gate"],
            "npc_ids": [],
        },
        {
            "id": "yard",
            "name": "Yard",
            "description": "An empty yard.",
            "exits": [],
            "item_ids": [],
            "door_ids": [],
            "npc_ids": [],
        },
    ],
    "items": [
        {"id": "gem", "name": "Gem", "description": "A gem."},
        {"id": "key", "name": "Gate key", "description": "A small key."},
    ],
    "doors": [
        {
            "id": "gate",
            "name": "Gate",
            "location_id": "square",
            "locked": True,
            "opened": False,
            "key_item_id": "key",
        }
    ],
    "entities": [],
    "quests": [],
    "events": [],
}


def _bind(client):
    assert client.post("/api/v1/worlds", json=WORLD).status_code in (200, 201)
    bound = client.post("/api/v1/campaigns/camp-1/world", json={"world_id": "preview_world"})
    assert bound.status_code == 200, bound.text


def test_action_catalogue_lists_verbs_and_targets(client):
    response = client.get("/api/v1/actions")
    assert response.status_code == 200
    catalogue = {row["name"]: row for row in response.json()}
    assert "take" in catalogue and "custom" in catalogue
    assert catalogue["take"]["target"] == "required"
    assert "拾取" in catalogue["take"]["verbs"]
    assert catalogue["look"]["target"] == "none"


def test_preview_reports_the_refusal_without_writing(client):
    _bind(client)
    response = client.post(
        "/api/v1/campaigns/camp-1/actions/preview",
        json={"action": "open", "target_id": "gate"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "door_locked"
    assert body["state_version"] == 0
    assert body["changed"] is False
    # Nothing was committed: still version 0 with the door shut.
    state = client.get("/api/v1/campaigns/camp-1/state").json()
    assert state["version"] == 0
    assert state["doors"]["gate"] is False


def test_preview_lists_effects_for_an_allowed_action_without_committing(client):
    _bind(client)
    response = client.post(
        "/api/v1/campaigns/camp-1/actions/preview",
        json={"action": "take", "target_id": "gem"},
    )
    body = response.json()
    assert body["ok"] is True
    assert body["changed"] is True
    assert any(effect["kind"] == "give_item" for effect in body["effects"])
    state = client.get("/api/v1/campaigns/camp-1/state").json()
    assert state["version"] == 0
    assert state["inventory"] == {}


def test_preview_rejects_a_mismatched_campaign_id(client):
    _bind(client)
    response = client.post(
        "/api/v1/campaigns/camp-1/actions/preview",
        json={"campaign_id": "other", "action": "look"},
    )
    assert response.status_code == 400


def test_preview_needs_a_bound_campaign(client):
    response = client.post(
        "/api/v1/campaigns/ghost/actions/preview", json={"action": "look"}
    )
    assert response.status_code == 404
