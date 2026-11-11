"""Route contracts for the interaction planner endpoints."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


V1 = "/api/v1"

WORLD = {
    "world_id": "w_plan",
    "name": "Plan Village",
    "description": "",
    "start_location_id": "square",
    "locations": [
        {"id": "square", "name": "Square", "description": "", "item_ids": ["brass_key"]},
        {"id": "depot", "name": "Depot", "description": ""},
    ],
    "items": [{"id": "brass_key", "name": "Brass Key"}],
    "doors": [
        {
            "id": "depot_door",
            "name": "Depot Door",
            "location_id": "square",
            "locked": True,
            "key_item_id": "brass_key",
        }
    ],
}


def _bound(client, campaign_id="camp-plan") -> None:
    assert client.post(f"{V1}/worlds", json=WORLD).status_code in (200, 201)
    res = client.post(
        f"{V1}/campaigns/{campaign_id}/world",
        json={"world_id": "w_plan", "reset": True},
    )
    assert res.status_code in (200, 201), res.text


def test_plan_lists_allowed_and_blocked_candidates(client):
    _bound(client)
    res = client.post(f"{V1}/worlds/w_plan/plan?campaign_id=camp-plan")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["location_id"] == "square"
    allowed = {(row["action"], row["target"]) for row in body["allowed"]}
    blocked = {(row["action"], row["target"]): row["code"] for row in body["blocked"]}
    assert ("take", "brass_key") in allowed
    assert blocked[("open", "depot_door")] == "door_locked"


def test_plan_without_a_campaign_uses_a_throwaway_state(client):
    assert client.post(f"{V1}/worlds", json=WORLD).status_code in (200, 201)
    res = client.post(f"{V1}/worlds/w_plan/plan")
    assert res.status_code == 200, res.text
    assert res.json()["campaign_id"] == "__preview__"
    # Nothing was bound as a side effect.
    assert client.get(f"{V1}/campaigns/__preview__/state").status_code == 404


def test_preview_actions_shows_changes_and_keeps_state(client):
    _bound(client)
    body = {
        "campaign_id": "camp-plan",
        "steps": [
            {"action": "take", "target": "brass_key"},
            {"action": "open", "target": "depot_door"},
        ],
    }
    res = client.post(f"{V1}/worlds/w_plan/preview-actions", json=body)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["simulated"] is True
    assert len(data["steps"]) == 2
    fields = {(c["field"], c["target"]) for c in data["steps"][0]["changes"]}
    assert ("inventory", "brass_key") in fields
    assert data["steps"][1]["changes"], "opening the door should report a change"

    state = client.get(f"{V1}/campaigns/camp-plan/state").json()
    assert state["inventory"] == {} and state["doors"]["depot_door"] is False
    assert state["version"] == 0


def test_preview_actions_rejects_bad_chains(client):
    _bound(client)
    res = client.post(
        f"{V1}/worlds/w_plan/preview-actions",
        json={"campaign_id": "camp-plan", "steps": []},
    )
    assert res.status_code == 422
    res = client.post(
        f"{V1}/worlds/w_plan/preview-actions",
        json={"campaign_id": "nope", "steps": [{"action": "look", "target": ""}]},
    )
    assert res.status_code == 404


def test_explain_shows_the_key_and_unknown_ids_404(client):
    _bound(client)
    res = client.post(
        f"{V1}/worlds/w_plan/explain",
        json={"campaign_id": "camp-plan", "action": "open", "target": "depot_door"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False and body["code"] == "door_locked"
    assert body["has_plan"] is True
    assert body["requirements"][0]["target"] == "brass_key"

    bad = client.post(
        f"{V1}/worlds/w_plan/explain",
        json={"campaign_id": "camp-plan", "action": "open", "target": "ghost"},
    )
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
