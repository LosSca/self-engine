"""HTTP surface for world-authored actions (/api/v1/.../custom-actions)."""
import pytest
from fastapi.testclient import TestClient

from tests.support import village_world_data

API = "/api/v1"

PRAY = {
    "id": "pray",
    "label": "祈祷",
    "description": "You press your palms together.",
    "verbs": ["祈祷", "pray"],
    "target": "none",
    "requires": [{"kind": "has_item", "target": "iron_key"}],
    "effects": [{"kind": "set_flag", "target": "prayed", "value": True}],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    from app.main import app

    return TestClient(app)


@pytest.fixture
def world(client):
    created = client.post(f"{API}/worlds", json=village_world_data())
    assert created.status_code == 200, created.text
    return "demo_village"


def test_custom_action_can_be_created_listed_and_deleted(client, world):
    created = client.put(f"{API}/worlds/{world}/custom-actions/pray", json=PRAY)
    assert created.status_code == 200, created.text
    assert [item["id"] for item in created.json()["custom_actions"]] == ["pray"]

    fetched = client.get(f"{API}/worlds/{world}")
    assert fetched.json()["custom_actions"][0]["verbs"] == ["祈祷", "pray"]

    updated = client.put(
        f"{API}/worlds/{world}/custom-actions/pray",
        json={**PRAY, "label": "默祷"},
    )
    assert updated.status_code == 200
    assert updated.json()["custom_actions"][0]["label"] == "默祷"
    assert len(updated.json()["custom_actions"]) == 1

    deleted = client.delete(f"{API}/worlds/{world}/custom-actions/pray")
    assert deleted.status_code == 200
    assert deleted.json()["custom_actions"] == []
    assert client.delete(f"{API}/worlds/{world}/custom-actions/pray").status_code == 404


def test_a_mismatched_path_id_is_rejected(client, world):
    response = client.put(f"{API}/worlds/{world}/custom-actions/chant", json=PRAY)
    assert response.status_code == 400
    assert "does not match the path id" in response.text


def test_a_verb_that_shadows_a_built_in_action_is_rejected(client, world):
    response = client.put(
        f"{API}/worlds/{world}/custom-actions/pray",
        json={**PRAY, "verbs": ["open"]},
    )
    assert response.status_code == 400
    assert "already mapped to a built-in action" in response.text
    # The stored world is unchanged by the rejected edit.
    assert client.get(f"{API}/worlds/{world}").json()["custom_actions"] == []


def test_a_condition_on_an_unknown_item_is_rejected(client, world):
    response = client.put(
        f"{API}/worlds/{world}/custom-actions/pray",
        json={**PRAY, "requires": [{"kind": "has_item", "target": "ghost_item"}]},
    )
    assert response.status_code == 400
    assert "unknown item 'ghost_item'" in response.text


def test_an_unknown_effect_kind_is_rejected_by_the_model(client, world):
    response = client.put(
        f"{API}/worlds/{world}/custom-actions/pray",
        json={**PRAY, "effects": [{"kind": "run_python", "target": "print(1)"}]},
    )
    assert response.status_code == 422


def test_the_action_type_catalogue_includes_custom(client):
    response = client.get(f"{API}/action-types")
    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert "custom" in names


def test_a_world_action_runs_through_the_text_endpoint(client, world):
    client.put(f"{API}/worlds/{world}/custom-actions/pray", json=PRAY)
    assert client.post(
        f"{API}/campaigns/camp-1/world", json={"world_id": world}
    ).status_code in (200, 201)

    blocked = client.post(
        f"{API}/campaigns/camp-1/text-actions",
        json={"text": "pray", "request_id": "r1", "narrate": False},
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["status"] == "rejected"
    assert blocked.json()["result"]["code"] == "condition_failed"

    taken = client.post(
        f"{API}/campaigns/camp-1/text-actions",
        json={"text": "pick up the iron key", "request_id": "r2", "narrate": False},
    )
    assert taken.json()["status"] == "applied"

    prayed = client.post(
        f"{API}/campaigns/camp-1/text-actions",
        json={"text": "祈祷", "request_id": "r3", "narrate": False},
    )
    assert prayed.status_code == 200, prayed.text
    body = prayed.json()
    assert body["status"] == "applied"
    assert body["parsed"]["action"] == "custom"
    assert body["parsed"]["params"]["action_id"] == "pray"

    state = client.get(f"{API}/campaigns/camp-1/state").json()
    assert state["flags"]["prayed"] is True
