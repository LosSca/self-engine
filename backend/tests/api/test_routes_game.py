import pytest

from tests.conftest import DEFAULT_OPENING as OPENING
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    from app.main import app
    return TestClient(app)


def test_world_graph_endpoint(client):
    r = client.get("/api/game/test-campaign/world-graph")
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data
    assert "links" in data
    assert isinstance(data["nodes"], list)
    assert isinstance(data["links"], list)


def test_graph_search_endpoint(client):
    r = client.get("/api/game/camp-1/graph-search?q=who+is+the+knight")
    assert r.status_code == 200
    data = r.json()
    assert "facts" in data
    assert isinstance(data["facts"], list)


def test_inventory_reads_the_authoritative_state_when_a_world_is_bound(client):
    scenario = client.post(
        "/api/scenarios/", json={"title": "Inventory", "language": "zh", "opening_narrative": OPENING}
    ).json()
    world = {
        "world_id": "inventory_world",
        "scenario_id": scenario["id"],
        "name": "Inventory world",
        "start_location_id": "square",
        "locations": [
            {"id": "square", "name": "Square", "item_ids": ["stone"]},
        ],
        "items": [{"id": "stone", "name": "Stone", "description": "A smooth stone."}],
        "doors": [],
        "entities": [],
        "quests": [],
        "events": [],
    }
    assert client.post("/api/v1/worlds", json=world).status_code == 200
    campaign = client.post(
        f"/api/scenarios/{scenario['id']}/campaigns", json={"player_name": "Tester"}
    ).json()
    assert client.post(
        f"/api/v1/campaigns/{campaign['id']}/world",
        json={"world_id": "inventory_world"},
    ).status_code == 200
    taken = client.post(
        f"/api/v1/campaigns/{campaign['id']}/text-actions",
        json={"text": "take stone", "request_id": "inv-1", "narrate": False},
    ).json()
    assert taken["status"] == "applied"

    items = client.get(f"/api/game/{campaign['id']}/inventory").json()
    assert items == [
        {
            "id": "stone",
            "name": "Stone",
            "category": "misc",
            "source": "A smooth stone.",
            "status": "carried",
            "quantity": 1,
        }
    ]

    blocked = client.post(
        f"/api/game/{campaign['id']}/inventory",
        json={"name": "Stone", "action": "discard"},
    )
    assert blocked.status_code == 409
    assert client.get(f"/api/v1/campaigns/{campaign['id']}/state").json()["inventory"] == {
        "stone": 1
    }
