import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("ENGINE_NARRATOR", "template")
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app)


def _scenario(client, title="\u96fe\u6e2f"):
    from app.db.scenario_store import ScenarioStore
    import os

    store = ScenarioStore(os.environ["SCENARIO_DB_PATH"])
    try:
        return store.create_scenario(title=title)
    finally:
        store.close()


def _seed_narration(campaign_id, text, request_id="req-1", version=1):
    from app.db.game_state_store import GameStateStore
    from app.models.game import GameState
    import os

    state = GameState(campaign_id=campaign_id, world_id="demo_village", version=version)
    store = GameStateStore(os.environ["GAME_STATE_DB_PATH"])
    try:
        store.bind_campaign(
            state,
            events=[
                {
                    "event_type": "narrator.response",
                    "request_id": request_id,
                    "payload": {"text": text},
                    "state_version": version,
                }
            ],
        )
    finally:
        store.close()


def test_extract_proposes_without_writing(client):
    scenario = _scenario(client)
    _seed_narration("camp-1", "The Old Mill is a ruined tower. You stand at the crossroads.")
    response = client.post(
        f"/api/v1/scenarios/{scenario.id}/knowledge/extract",
        json={"campaign_id": "camp-1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is False
    assert body["turns_read"] == 1
    assert [entry["content"] for entry in body["entries"]] == [
        "The Old Mill is a ruined tower.",
        "You stand at the crossroads.",
    ]
    stored = client.get(f"/api/v1/scenarios/{scenario.id}/knowledge").json()
    assert stored == []


def test_extract_can_apply_and_is_idempotent(client):
    scenario = _scenario(client)
    _seed_narration("camp-1", "The keeper watches the gate.")
    first = client.post(
        f"/api/v1/scenarios/{scenario.id}/knowledge/extract",
        json={"campaign_id": "camp-1", "apply": True},
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["created_ids"]) == 1
    second = client.post(
        f"/api/v1/scenarios/{scenario.id}/knowledge/extract",
        json={"campaign_id": "camp-1", "apply": True},
    )
    assert second.json()["created_ids"] == []
    stored = client.get(f"/api/v1/scenarios/{scenario.id}/knowledge").json()
    assert [entry["content"] for entry in stored] == ["The keeper watches the gate."]


def test_extract_rejects_an_unknown_scenario(client):
    response = client.post(
        "/api/v1/scenarios/nope/knowledge/extract", json={"campaign_id": "camp-1"}
    )
    assert response.status_code == 404
    assert "unknown scenario" in response.text


def test_refine_without_a_model_is_refused(client, monkeypatch):
    scenario = _scenario(client)
    from app.services import ai_generator

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: None)
    response = client.post(
        f"/api/v1/scenarios/{scenario.id}/knowledge/extract",
        json={"campaign_id": "camp-1", "refine": True},
    )
    assert response.status_code == 503
    assert "\u672a\u914d\u7f6e" in response.text
