"""HTTP surface of the rule-driven engine (/api/v1)."""
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


def _create_world(client, payload=None):
    return client.post(f"{API}/worlds", json=payload or village_world_data())


def test_create_list_and_get_world(client):
    created = _create_world(client)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "saved"

    listed = client.get(f"{API}/worlds")
    assert listed.status_code == 200
    assert [item["world_id"] for item in listed.json()] == ["demo_village"]

    fetched = client.get(f"{API}/worlds/demo_village")
    assert fetched.status_code == 200
    assert fetched.json()["start_location_id"] == "village_square"
    assert client.get(f"{API}/worlds/ghost").status_code == 404


def test_invalid_world_is_rejected_with_readable_errors(client):
    payload = village_world_data()
    payload["locations"][0]["exits"][0]["target_id"] = "nowhere"
    response = _create_world(client, payload)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert any("nowhere" in error for error in detail), detail


def test_validate_endpoint_reports_errors_without_saving(client):
    payload = village_world_data()
    payload["doors"][0]["key_item_id"] = "ghost_item"
    response = client.post(f"{API}/worlds/validate", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("ghost_item" in error for error in body["errors"]), body
    assert client.get(f"{API}/worlds").json() == []


def test_action_flow_over_http(client):
    _create_world(client)
    bound = client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})
    assert bound.status_code == 200, bound.text
    assert bound.json()["version"] == 0

    def act(request_id, action, target=None):
        return client.post(
            f"{API}/campaigns/camp-1/actions",
            json={
                "campaign_id": "camp-1",
                "request_id": request_id,
                "action": action,
                "target_id": target,
            },
        )

    take = act("req-take", "take", "iron_key")
    assert take.status_code == 200, take.text
    body = take.json()
    assert body["status"] == "applied"
    assert body["ok"] is True
    assert body["state_version"] == 1
    assert body["state"]["inventory"] == {"iron_key": 1}

    duplicate = act("req-take", "take", "iron_key")
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate.json()["state_version"] == 1

    open_door = act("req-open", "open", "warehouse_door")
    body = open_door.json()
    assert body["status"] == "applied"
    assert body["state_version"] == 2
    assert body["state"]["doors"]["warehouse_door"] is True
    assert body["quests"]["open_the_warehouse"] == "completed"
    assert body["narrative"]["status"] == "ok"

    state = client.get(f"{API}/campaigns/camp-1/state").json()
    assert state["version"] == 2
    assert state["inventory"] == {"iron_key": 1, "blessing_token": 1}

    events = client.get(f"{API}/campaigns/camp-1/events").json()
    assert events
    assert {event["state_version"] for event in events} == {1, 2}

    stored = client.get(f"{API}/actions/req-take").json()
    assert stored["status"] == "applied"
    assert stored["request"]["target_id"] == "iron_key"
    assert client.get(f"{API}/actions/ghost").status_code == 404


def test_locked_door_is_rejected_over_http(client):
    _create_world(client)
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})
    response = client.post(
        f"{API}/campaigns/camp-1/actions",
        json={
            "campaign_id": "camp-1",
            "request_id": "req-open",
            "action": "open",
            "target_id": "warehouse_door",
        },
    )
    body = response.json()
    assert body["status"] == "rejected"
    assert body["code"] == "door_locked"
    assert body["state_version"] == 0
    assert body["state"]["doors"]["warehouse_door"] is False


def test_campaign_validation_errors(client):
    _create_world(client)
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})
    mismatch = client.post(
        f"{API}/campaigns/camp-1/actions",
        json={"campaign_id": "camp-2", "request_id": "r", "action": "look"},
    )
    assert mismatch.status_code == 400

    missing = client.post(
        f"{API}/campaigns/camp-9/actions",
        json={"campaign_id": "camp-9", "request_id": "r", "action": "look"},
    )
    assert missing.status_code == 404
    assert client.get(f"{API}/campaigns/camp-9/state").status_code == 404

    bad_action = client.post(
        f"{API}/campaigns/camp-1/actions",
        json={"campaign_id": "camp-1", "request_id": "r", "action": "fly"},
    )
    assert bad_action.status_code == 422


def test_event_validate_and_simulate_do_not_touch_real_state(client):
    _create_world(client)
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})
    client.post(
        f"{API}/campaigns/camp-1/actions",
        json={
            "campaign_id": "camp-1",
            "request_id": "req-take",
            "action": "take",
            "target_id": "iron_key",
        },
    )

    validated = client.post(f"{API}/worlds/demo_village/events/keeper_alert/validate")
    assert validated.status_code == 200
    assert validated.json() == {"valid": True, "errors": []}

    simulated = client.post(
        f"{API}/worlds/demo_village/events/keeper_alert/simulate",
        params={"campaign_id": "camp-1"},
    )
    assert simulated.status_code == 200
    assert simulated.json()["status"] == "not_applicable"

    state = client.get(f"{API}/campaigns/camp-1/state").json()
    assert state["version"] == 1
    assert "warehouse_opened" not in state["flags"]
    assert client.post(f"{API}/worlds/demo_village/events/ghost/validate").status_code == 404

# ------------------------------------------------- story mode over HTTP
def test_story_endpoint_reports_free_mode_by_default(client):
    _create_world(client)
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})

    res = client.get(f"{API}/campaigns/camp-1/story")
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "free"
    assert body["entered"] == []
    assert body["active"] is None


def test_story_endpoint_tracks_storyline_progress(client, tmp_path):
    from app.db.scenario_store import ScenarioStore
    from app.models.knowledge import PlayConfig, Storyline, StoryNode

    with ScenarioStore(str(tmp_path / "scenarios.db")) as store:
        scenario = store.create_scenario(title="仓库线", language="zh")
        store.set_play_config(
            scenario.id,
            PlayConfig(
                scenario_id=scenario.id,
                mode="storyline",
                storyline=Storyline(
                    name="仓库线",
                    nodes=[
                        StoryNode(id="arrive", title="抵达村庄"),
                        StoryNode(
                            id="open",
                            title="打开仓库",
                            prereq=["arrive"],
                            conditions=[{"kind": "door_open", "target": "warehouse_door"}],
                        ),
                    ],
                ),
            ),
        )

    payload = village_world_data()
    payload["scenario_id"] = scenario.id
    assert _create_world(client, payload).status_code == 200
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})

    def act(request_id, action, target=None):
        return client.post(
            f"{API}/campaigns/camp-1/actions",
            json={
                "campaign_id": "camp-1",
                "request_id": request_id,
                "action": action,
                "target_id": target,
            },
        ).json()

    taken = act("req-take", "take", "iron_key")
    assert taken["story_nodes_entered"] == ["arrive"]

    opened = act("req-open", "open", "warehouse_door")
    assert opened["story_nodes_entered"] == ["open"]

    story = client.get(f"{API}/campaigns/camp-1/story").json()
    assert story["mode"] == "storyline"
    assert story["entered"] == ["arrive", "open"]
    assert story["active"] == "open"
    assert story["storyline"]["name"] == "仓库线"
    assert [node["id"] for node in story["storyline"]["nodes"]] == ["arrive", "open"]
