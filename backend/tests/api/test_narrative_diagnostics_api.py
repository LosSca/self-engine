"""End-to-end: a turn stores what the narrator saw, without changing the turn."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenario.db"))
    monkeypatch.setenv("ENGINE_DEBUG_TOOLS", "1")
    import importlib

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app) as client:
        yield client


def _create_and_bind(client) -> str:
    world = {
        "world_id": "diag_world",
        "name": "Diagnostics",
        "description": "",
        "start_location_id": "square",
        "locations": [
            {
                "id": "square",
                "name": "Square",
                "description": "A square.",
                "exits": [{"target_id": "hall", "label": "north"}],
            },
            {
                "id": "hall",
                "name": "Hall",
                "description": "A hall.",
                "exits": [{"target_id": "square", "label": "south"}],
            },
        ],
    }
    created = client.post("/api/v1/worlds", json=world)
    assert created.status_code in (200, 201), created.text
    bound = client.post(
        "/api/v1/campaigns/camp-1/world",
        json={"world_id": "diag_world", "reset": True},
    )
    assert bound.status_code == 200, bound.text
    return "diag_world"


def test_a_turn_stores_narration_diagnostics(client):
    _create_and_bind(client)
    response = client.post(
        "/api/v1/campaigns/camp-1/actions",
        json={
            "request_id": "req-1",
            "campaign_id": "camp-1",
            "action": "move",
            "target_id": "hall",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    diagnostics = (body.get("narrative") or {}).get("diagnostics")
    assert isinstance(diagnostics, dict), body.get("narrative")
    # The template narrator runs in tests: the capture still says so.
    assert diagnostics["provider"] == "template"

    events = client.get("/api/v1/campaigns/camp-1/events", params={"limit": 20})
    assert events.status_code == 200, events.text
    rows = events.json()
    if isinstance(rows, dict):
        rows = rows.get("events") or rows.get("items") or []
    narration = [r for r in rows if r.get("event_type") == "narrator.response"]
    assert narration, rows
    payload = narration[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["diagnostics"]["provider"] == "template"

