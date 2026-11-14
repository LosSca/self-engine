"""Which lane answers a campaign, and does the engine lane keep its guarantees."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LLM_TRACE_DB_PATH", str(tmp_path / "traces.db"))
    from app.main import app

    return TestClient(app)


@pytest.fixture
def campaign(client):
    scenario = client.post(
        "/api/scenarios",
        json={"title": "Lane Test", "language": "en", "opening_narrative": "A dim corridor."},
    ).json()
    created = client.post(
        f"/api/scenarios/{scenario['id']}/campaigns", json={"player_name": "Tester"}
    )
    assert created.status_code == 201
    return scenario["id"], created.json()["id"]


def test_unbound_campaign_stays_on_the_narrative_lane(client, campaign):
    _, campaign_id = campaign
    lane = client.get(f"/api/v1/campaigns/{campaign_id}/lane")
    assert lane.status_code == 200
    body = lane.json()
    assert body["lane"] == "narrative"
    assert body["bound"] is False
    # And no authoritative state exists yet, so the legacy lane is the only one.
    assert client.get(f"/api/v1/campaigns/{campaign_id}/state").status_code == 404


def test_bound_campaign_switches_to_the_engine_lane(client, campaign):
    scenario_id, campaign_id = campaign
    world = client.post(
        "/api/v1/worlds",
        json={
            "world_id": "lane_world",
            "scenario_id": scenario_id,
            "name": "Lane World",
            "start_location_id": "square",
            "locations": [
                {"id": "square", "name": "Square", "item_ids": ["stone"]},
                {"id": "alley", "name": "Alley"},
            ],
            "items": [{"id": "stone", "name": "Stone"}],
            "doors": [],
            "entities": [],
            "quests": [],
            "events": [],
        },
    )
    assert world.status_code in (200, 201), world.text

    bound = client.post(
        f"/api/v1/campaigns/{campaign_id}/world", json={"world_id": "lane_world"}
    )
    assert bound.status_code == 200, bound.text

    lane = client.get(f"/api/v1/campaigns/{campaign_id}/lane").json()
    assert lane["lane"] == "engine"
    assert lane["bound"] is True
    assert lane["world_id"] == "lane_world"
    assert lane["version"] == 0


def test_engine_lane_is_idempotent_and_rejects_rule_violations(client, campaign):
    scenario_id, campaign_id = campaign
    client.post(
        "/api/v1/worlds",
        json={
            "world_id": "lane_world",
            "scenario_id": scenario_id,
            "name": "Lane World",
            "start_location_id": "square",
            "locations": [
                {"id": "square", "name": "Square", "item_ids": ["stone"], "door_ids": ["gate"]}
            ],
            "items": [{"id": "stone", "name": "Stone"}],
            "doors": [
                {
                    "id": "gate",
                    "name": "Gate",
                    "location_id": "square",
                    "locked": True,
                    "key_item_id": "stone",
                }
            ],
            "entities": [],
            "quests": [],
            "events": [],
        },
    )
    client.post(f"/api/v1/campaigns/{campaign_id}/world", json={"world_id": "lane_world"})

    locked_envelope = client.post(
        f"/api/v1/campaigns/{campaign_id}/text-actions",
        json={"text": "open the gate", "request_id": "lane-r1", "narrate": False},
    ).json()
    locked = locked_envelope["result"]
    assert locked_envelope["status"] == "rejected"
    assert locked["code"] == "door_locked"
    assert locked["state_version"] == 0

    taken = client.post(
        f"/api/v1/campaigns/{campaign_id}/text-actions",
        json={"text": "take the stone", "request_id": "lane-r2", "narrate": False},
    ).json()["result"]
    assert taken["status"] == "applied"
    assert taken["state_version"] == 1
    assert taken["state"]["inventory"]["stone"] == 1

    # Same request again: a replay, never a second stone.
    again_envelope = client.post(
        f"/api/v1/campaigns/{campaign_id}/text-actions",
        json={"text": "take the stone", "request_id": "lane-r2", "narrate": False},
    ).json()
    assert again_envelope["status"] == "duplicate"
    # Replays report "duplicate" at both levels, like the structured endpoint.
    assert again_envelope["result"]["status"] == "duplicate"
    assert again_envelope["result"]["state_version"] == 1
    state = client.get(f"/api/v1/campaigns/{campaign_id}/state").json()
    assert state["inventory"]["stone"] == 1
    assert state["version"] == 1

    opened = client.post(
        f"/api/v1/campaigns/{campaign_id}/text-actions",
        json={"text": "open the gate", "request_id": "lane-r3", "narrate": False},
    ).json()["result"]
    assert opened["status"] == "applied"
    assert opened["state"]["doors"]["gate"] is True
    assert opened["state_version"] == 2
