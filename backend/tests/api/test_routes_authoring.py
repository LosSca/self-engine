"""Route contracts for the conversational authoring lane."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


V1 = "/api/v1"

WORLD = {
    "world_id": "w_talk",
    "name": "Talk Village",
    "description": "",
    "start_location_id": "square",
    "locations": [{"id": "square", "name": "Square", "description": ""}],
    "items": [],
    "doors": [],
}


class _Router:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages, max_tokens=0, temperature=0.0):
        # ai_generator._ask expects JSON text, not a dict.
        import json as _json

        return _json.dumps(self.payload, ensure_ascii=False)


def _install(monkeypatch, payload):
    from app.services import ai_generator

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: _Router(payload))


def _make_world(client):
    res = client.post(f"{V1}/worlds", json=WORLD)
    assert res.status_code in (200, 201), res.text


def test_propose_returns_a_staged_proposal(client, monkeypatch):
    _make_world(client)
    _install(monkeypatch, {
        "reply": "加一条河边小路",
        "ops": [
            {
                "kind": "add_location",
                "data": {
                    "id": "river_path",
                    "name": "River Path",
                    "description": "Mud.",
                    "exits": [{"target_id": "square"}],
                },
            },
            {"kind": "set_note", "data": {"title": "River", "content": "Cold water."}},
        ],
    })
    res = client.post(
        f"{V1}/worlds/w_talk/authoring/propose",
        json={"message": "加一条去河边的小路"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "proposed"
    assert body["reply"] == "加一条河边小路"
    assert [op["kind"] for op in body["ops"]] == ["add_location", "add_knowledge"]
    assert body["changes"] == [{"label": "地点", "before": 1, "after": 2}]
    assert body["knowledge_titles"] == ["River"]

    # Nothing was written: the world still has one location.
    world = client.get(f"{V1}/worlds/w_talk").json()
    assert [loc["id"] for loc in world["locations"]] == ["square"]


def test_propose_needs_a_model(client, monkeypatch):
    _make_world(client)
    from app.services import ai_generator

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: None)
    res = client.post(
        f"{V1}/worlds/w_talk/authoring/propose",
        json={"message": "加一个村庄"},
    )
    assert res.status_code == 503
    assert "未配置可用模型" in res.json()["detail"]


def test_commit_writes_the_approved_proposal(client, monkeypatch):
    _make_world(client)
    payload = {
        "reply": "好",
        "ops": [
            {
                "kind": "add_location",
                "data": {
                    "id": "river_path",
                    "name": "River Path",
                    "description": "Mud.",
                    "exits": [{"target_id": "square"}],
                },
            }
        ],
    }
    _install(monkeypatch, payload)
    client.post(f"{V1}/worlds/w_talk/authoring/propose", json={"message": "x"})

    import json as _json

    from app.models.game import WorldDefinition
    from app.services import authoring_session

    world = WorldDefinition.model_validate(client.get(f"{V1}/worlds/w_talk").json())
    _, proposal = authoring_session.propose(payload, world, [])
    # Round-trip the staged proposal exactly as a client would.
    body = {
        "proposal": {
            "world": _json.loads(proposal["world"].model_dump_json()),
            "knowledge": [
                _json.loads(entry.model_dump_json()) for entry in proposal["knowledge"]
            ],
        }
    }
    res = client.post(f"{V1}/worlds/w_talk/authoring/commit", json=body)
    assert res.status_code == 200, res.text
    result = res.json()
    assert result["world_saved"] is True
    assert result["world_id"] == "w_talk"
    world_after = client.get(f"{V1}/worlds/w_talk").json()
    assert "river_path" in [loc["id"] for loc in world_after["locations"]]
