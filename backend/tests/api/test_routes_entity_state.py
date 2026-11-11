"""Live entity positions in the map projection and in the saved state."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models.game import (
    Condition,
    Effect,
    EntityDef,
    ExitDef,
    GameEventDef,
    LocationDef,
    WorldDefinition,
)

API = "/api/v1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    from app.main import app

    return TestClient(app)


def patrol_world() -> dict:
    world = WorldDefinition(
        world_id="patrol_world",
        name="Patrol",
        start_location_id="square",
        locations=[
            LocationDef(
                id="square",
                name="Square",
                exits=[ExitDef(target_id="hall", label="east")],
            ),
            LocationDef(
                id="hall",
                name="Hall",
                exits=[ExitDef(target_id="square", label="west")],
            ),
        ],
        entities=[
            EntityDef(id="guard", name="Guard", location_id="square", kind="npc"),
            EntityDef(
                id="statue",
                name="Statue",
                location_id="square",
                kind="object",
            ),
        ],
        events=[
            GameEventDef(
                id="guard_patrols",
                name="The guard walks his round",
                trigger="action",
                trigger_action="look",
                conditions=[Condition(kind="entity_at", target="guard", value="square")],
                effects=[Effect(kind="move_entity", target="guard", value="hall")],
                repeat="always",
            )
        ],
    )
    return world.model_dump(mode="json")


def _bind(client) -> str:
    assert client.post(f"{API}/worlds", json=patrol_world()).status_code == 200
    assert client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "patrol_world"}).status_code == 200
    return "camp-1"


def _locate(map_payload: dict) -> dict:
    return {loc["id"]: [npc["id"] for npc in loc["npcs"]] for loc in map_payload["locations"]}


def test_the_map_without_a_campaign_shows_the_authored_layout(client):
    _bind(client)

    body = client.get(f"{API}/worlds/patrol_world/map").json()

    assert _locate(body) == {"square": ["guard"], "hall": []}
    assert {entity["id"]: entity["location_id"] for entity in body["entities"]}["guard"] == "square"


def test_the_map_with_a_campaign_shows_live_positions(client):
    campaign_id = _bind(client)

    turned = client.post(
        f"{API}/campaigns/{campaign_id}/actions",
        json={"campaign_id": campaign_id, "request_id": "r1", "action": "look"},
    )
    assert turned.status_code == 200, turned.text
    assert turned.json()["status"] == "applied"

    live = client.get(f"{API}/worlds/patrol_world/map", params={"campaign_id": campaign_id}).json()
    assert _locate(live) == {"square": [], "hall": ["guard"]}
    guard = next(entity for entity in live["entities"] if entity["id"] == "guard")
    assert guard["location_id"] == "hall"

    # The authored document is untouched: the world itself still says square.
    authored = client.get(f"{API}/worlds/patrol_world/map").json()
    assert _locate(authored) == {"square": ["guard"], "hall": []}


def test_a_map_for_an_unknown_campaign_falls_back_to_the_authored_layout(client):
    _bind(client)

    body = client.get(
        f"{API}/worlds/patrol_world/map", params={"campaign_id": "nope"}
    ).json()

    assert _locate(body) == {"square": ["guard"], "hall": []}


def test_entity_state_survives_a_reload_of_the_campaign_state(client):
    campaign_id = _bind(client)
    client.post(
        f"{API}/campaigns/{campaign_id}/actions",
        json={"campaign_id": campaign_id, "request_id": "r1", "action": "look"},
    )

    state = client.get(f"{API}/campaigns/{campaign_id}/state").json()

    assert state["entities"]["guard"]["location_id"] == "hall"
    assert state["version"] == 1
    # Objects are never people: only the NPC got a record.
    assert "statue" not in state["entities"]


def test_an_entity_condition_is_visible_in_the_event_execution_log(client):
    campaign_id = _bind(client)
    client.post(
        f"{API}/campaigns/{campaign_id}/actions",
        json={"campaign_id": campaign_id, "request_id": "r1", "action": "look"},
    )

    executions = client.get(f"{API}/campaigns/{campaign_id}/event-executions").json()

    fired = [row for row in executions if row.get("status") == "fired"]
    assert any(row["event_id"] == "guard_patrols" for row in fired)
