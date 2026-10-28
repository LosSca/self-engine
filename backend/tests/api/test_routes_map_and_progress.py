"""Read-side projections: world map, quest progress, event execution history.

These endpoints exist so the editor and the debug console never have to
re-implement reference resolution or guess quest progress.
"""
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


def _bound(client, campaign_id: str = "camp-1"):
    assert client.post(f"{API}/worlds", json=village_world_data()).status_code == 200
    bound = client.post(
        f"{API}/campaigns/{campaign_id}/world", json={"world_id": "demo_village"}
    )
    assert bound.status_code == 200, bound.text


def _act(client, request_id, action, target=None, campaign_id="camp-1"):
    return client.post(
        f"{API}/campaigns/{campaign_id}/actions",
        json={
            "campaign_id": campaign_id,
            "request_id": request_id,
            "action": action,
            "target_id": target,
        },
    )


# ------------------------------------------------------------------- world map
def test_world_map_resolves_exits_doors_items_and_npcs(client):
    _bound(client)
    response = client.get(f"{API}/worlds/demo_village/map")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["start_location_id"] == "village_square"
    assert payload["counts"] == {
        "locations": 3,
        "items": 3,
        "doors": 1,
        # The demo fixture references "elder_mara" from a location but ships no
        # entity record for her, so the projection must report a dangling ref
        # instead of inventing one.
        "entities": 0,
        "quests": 1,
        "events": 2,
    }

    by_id = {location["id"]: location for location in payload["locations"]}
    square = by_id["village_square"]
    assert square["is_start"] is True
    assert [item["id"] for item in square["items"]] == ["iron_key"]
    assert square["npcs"] == [
        {"id": "elder_mara", "name": "elder_mara", "kind": "npc", "known": False}
    ]
    assert by_id["forest_edge"]["is_start"] is False

    door_exit = next(exit_def for exit_def in square["exits"] if exit_def["target_id"] == "warehouse")
    assert door_exit["label"] == "east"
    assert door_exit["door"]["id"] == "warehouse_door"
    assert door_exit["door"]["name"] == "warehouse door"
    assert door_exit["door"]["locked"] is True
    assert door_exit["door"]["key_item"]["name"] == "Iron Key"
    assert door_exit["requires_item"] is None

    plain_exit = next(exit_def for exit_def in square["exits"] if exit_def["target_id"] == "forest_edge")
    assert plain_exit["door"] is None


def test_world_map_resolves_configured_entities(client):
    payload = village_world_data()
    payload["world_id"] = "demo_village_entities"
    payload["entities"] = [
        {
            "id": "elder_mara",
            "name": "Elder Mara",
            "description": "The village elder, who counts everything twice.",
            "tags": ["npc"],
            "location_id": "village_square",
            "kind": "npc",
            "dialogue": ["Grain before nightfall, traveller."],
            "hostile": False,
        }
    ]
    assert client.post(f"{API}/worlds", json=payload).status_code == 200
    world_map = client.get(f"{API}/worlds/demo_village_entities/map").json()
    assert world_map["counts"]["entities"] == 1
    square = next(loc for loc in world_map["locations"] if loc["id"] == "village_square")
    assert square["npcs"] == [
        {"id": "elder_mara", "name": "Elder Mara", "kind": "npc", "known": True}
    ]


def test_world_map_is_read_only_and_unknown_world_is_404(client):
    _bound(client)
    before = client.get(f"{API}/worlds/demo_village/map").json()
    assert client.get(f"{API}/worlds/demo_village/map").json() == before
    assert client.get(f"{API}/worlds/ghost/map").status_code == 404
    # The projection never writes state: no campaign was even touched.
    assert client.get(f"{API}/campaigns/camp-1/state").json()["version"] == 0


# --------------------------------------------------------------- quest progress
def test_quest_progress_starts_inactive_then_tracks_objectives(client):
    _bound(client)
    initial = client.get(f"{API}/campaigns/camp-1/quest-progress")
    assert initial.status_code == 200, initial.text
    body = initial.json()
    assert body["version"] == 0
    quest = body["quests"][0]
    assert quest["quest_id"] == "open_the_warehouse"
    # Every enabled quest of a freshly bound world starts active at zero progress.
    assert quest["status"] == "active"
    assert quest["completed_objectives"] == 0
    assert quest["total_objectives"] == 2
    assert [objective["id"] for objective in quest["objectives"]] == ["find_key", "open_door"]
    assert quest["rewards"] == [{"kind": "give_item", "target": "blessing_token", "value": None, "amount": 1}]

    assert _act(client, "req-take", "take", "iron_key").json()["status"] == "applied"
    half = client.get(f"{API}/campaigns/camp-1/quest-progress").json()
    quest = half["quests"][0]
    assert quest["status"] == "active"
    assert half["active"] == ["open_the_warehouse"]
    objectives = {objective["id"]: objective for objective in quest["objectives"]}
    assert objectives["find_key"]["count"] == 1
    assert objectives["find_key"]["done"] is True
    assert objectives["open_door"]["count"] == 0
    assert objectives["open_door"]["done"] is False

    assert _act(client, "req-open", "open", "warehouse_door").json()["status"] == "applied"
    done = client.get(f"{API}/campaigns/camp-1/quest-progress").json()
    quest = done["quests"][0]
    assert quest["status"] == "completed"
    assert done["completed"] == ["open_the_warehouse"]
    assert quest["completed_objectives"] == 2

    assert client.get(f"{API}/campaigns/camp-9/quest-progress").status_code == 404


# ------------------------------------------------------------ event executions
def test_event_execution_history_reports_fired_events(client):
    _bound(client)
    assert client.get(f"{API}/campaigns/camp-1/event-executions").json() == []

    _act(client, "req-take", "take", "iron_key")
    _act(client, "req-open", "open", "warehouse_door")

    response = client.get(f"{API}/campaigns/camp-1/event-executions")
    assert response.status_code == 200, response.text
    history = response.json()

    fired = [entry for entry in history if entry["status"] == "fired"]
    assert {entry["event_id"] for entry in fired} == {"key_pickup_noted", "keeper_alert"}
    keeper = next(entry for entry in fired if entry["event_id"] == "keeper_alert")
    assert keeper["name"] == "The elder looks up"
    assert keeper["effects"][0]["kind"] == "set_flag"
    assert keeper["effects"][0]["target"] == "warehouse_opened"
    assert keeper["state_version"] == 2
    assert keeper["request_id"] == "req-open"

    only_fired = client.get(
        f"{API}/campaigns/camp-1/event-executions", params={"status": "fired", "limit": 1}
    ).json()
    assert len(only_fired) == 1
    assert only_fired[0]["event_id"] == "keeper_alert"  # newest first

    assert client.get(
        f"{API}/campaigns/camp-1/event-executions", params={"status": "failed"}
    ).json() == []


def test_event_execution_history_filters_by_state_version(client):
    _bound(client)
    _act(client, "req-take", "take", "iron_key")
    take_version = client.get(f"{API}/campaigns/camp-1/state").json()["version"]
    _act(client, "req-open", "open", "warehouse_door")

    later = client.get(
        f"{API}/campaigns/camp-1/event-executions",
        params={"after_version": take_version},
    ).json()
    assert [entry["event_id"] for entry in later] == ["keeper_alert"]