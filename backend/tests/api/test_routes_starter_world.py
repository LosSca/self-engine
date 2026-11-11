"""A campaign with no authored world must still enter the rules lane.

Before this, such a campaign silently fell back to the legacy LLM session: the
chat surface answered from memory while the rules engine had no state at all,
so the two systems could disagree about the same player.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name in ("GAME_STATE_DB_PATH", "SCENARIO_DB_PATH", "EVENT_DB_PATH"):
        monkeypatch.setenv(name, str(tmp_path / f"{name}.db"))
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ENGINE_UNIFIED_NARRATOR", "template")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def campaign(client):
    scenario = client.post("/api/scenarios/", json={"title": "Mist Inn", "language": "en"}).json()
    created = client.post(
        f"/api/scenarios/{scenario['id']}/campaigns", json={"player_name": "Tester"}
    ).json()
    return created["id"], scenario["id"]


def _door_state(client, campaign_id):
    state = client.get(f"/api/v1/campaigns/{campaign_id}/state").json()
    return state


def test_new_campaign_reports_a_bound_rules_lane(client, campaign):
    campaign_id, _scenario = campaign
    before = client.get(f"/api/v1/campaigns/{campaign_id}/lane").json()
    assert before["bound"] is False

    reply = client.post(
        "/api/game/action",
        json={"campaign_id": campaign_id, "action": "look around", "language": "en"},
    )
    assert reply.status_code == 200

    after = client.get(f"/api/v1/campaigns/{campaign_id}/lane").json()
    assert after["bound"] is True
    assert after["lane"] == "engine"
    assert after["world_id"].startswith("starter_")

    state = _door_state(client, campaign_id)
    assert state["world_id"] == after["world_id"]
    assert "q_first_steps" in state["quests"]


def test_locked_door_is_refused_then_opened_through_the_rules(client, campaign):
    campaign_id, _scenario = campaign
    client.post(
        "/api/game/action",
        json={"campaign_id": campaign_id, "action": "look around", "language": "en"},
    )

    refused = client.post(
        f"/api/v1/campaigns/{campaign_id}/text-actions",
        json={"text": "open the fallen gate", "request_id": "r-refused"},
    ).json()
    assert refused["status"] == "rejected"
    assert refused["result"]["code"] == "door_locked"
    assert _door_state(client, campaign_id)["doors"]["inner_door"] is False

    taken = client.post(
        f"/api/v1/campaigns/{campaign_id}/text-actions",
        json={"text": "take the traveler's key", "request_id": "r-take"},
    ).json()
    assert taken["status"] == "applied"

    opened = client.post(
        f"/api/v1/campaigns/{campaign_id}/text-actions",
        json={"text": "open the fallen gate", "request_id": "r-open"},
    ).json()
    assert opened["status"] == "applied"
    assert opened["result"]["state"]["doors"]["inner_door"] is True

    # Idempotency survives the generated world exactly like an authored one.
    replay = client.post(
        f"/api/v1/campaigns/{campaign_id}/text-actions",
        json={"text": "open the fallen gate", "request_id": "r-open"},
    ).json()
    assert replay["result"]["status"] == "duplicate"
    assert replay["result"]["state"]["inventory"] == taken["result"]["state"]["inventory"]


def test_generated_world_is_listed_and_still_editable(client, campaign):
    campaign_id, _scenario = campaign
    client.post(
        "/api/game/action",
        json={"campaign_id": campaign_id, "action": "look around", "language": "en"},
    )
    world_id = client.get(f"/api/v1/campaigns/{campaign_id}/lane").json()["world_id"]

    worlds = client.get("/api/v1/worlds").json()
    assert world_id in [world["world_id"] for world in worlds]

    world = client.get(f"/api/v1/worlds/{world_id}").json()
    assert world["scenario_id"] == _scenario
    assert set(world["locations"][0].keys()) >= {"id", "name"}
