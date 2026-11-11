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



def test_a_failed_narration_row_carries_the_capture(client):
    _create_and_bind(client)

    import app.api.routes_engine as routes_engine

    class _Broken:
        async def narrate(self, *args, **kwargs):
            from app.models.game import NarrativeDiagnostics, NarrativeResult

            return NarrativeResult(
                status="failed",
                error="RuntimeError: upstream exploded",
                diagnostics=NarrativeDiagnostics(
                    provider="deepseek",
                    model="deepseek-chat",
                    temperature=0.5,
                    max_tokens=200,
                    timeout=9.0,
                    language="English",
                    content_policy="standard",
                    preset_routed=False,
                    skipped=False,
                    blocks=[
                        {
                            "id": "canon:1",
                            "layer": "global",
                            "title": "The square",
                            "excerpt": "A square with a well.",
                        }
                    ],
                    fact_counts={"global": 1},
                ),
            )

    monkeypatched = getattr(routes_engine, "_get_narrator", None)
    assert monkeypatched is not None
    original = routes_engine._get_narrator
    routes_engine._get_narrator = lambda: _Broken()
    try:
        response = client.post(
            "/api/v1/campaigns/camp-1/actions",
            json={
                "request_id": "req-fail",
                "campaign_id": "camp-1",
                "action": "move",
                "target_id": "hall",
            },
        )
    finally:
        routes_engine._get_narrator = original
    assert response.status_code == 200, response.text
    body = response.json()
    # The turn still commits: a broken narrator never fails an action.
    assert body["status"] == "applied"

    events = client.get("/api/v1/campaigns/camp-1/events", params={"limit": 20})
    rows = events.json()
    if isinstance(rows, dict):
        rows = rows.get("events") or rows.get("items") or []
    failed = [r for r in rows if r.get("event_type") == "narrative.failed"]
    assert failed, rows
    payload = failed[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert "upstream exploded" in payload["error"]
    diagnostics = payload.get("diagnostics") or {}
    # Without this, a reviewer sees only that the model failed and not what it
    # had been given or which route produced the failure.
    assert diagnostics["provider"] == "deepseek"
    assert diagnostics["content_policy"] == "standard"
    assert diagnostics["fact_counts"] == {"global": 1}
    assert diagnostics["blocks"][0]["id"] == "canon:1"
