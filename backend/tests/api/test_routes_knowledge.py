"""API tests for authored knowledge, play modes and model presets (/api/v1)."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    from app.main import app

    return TestClient(app)


@pytest.fixture
def scenario_id(client):
    res = client.post("/api/scenarios/", json={"title": "雾港", "language": "zh"})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _entry(**overrides):
    body = {"layer": "global", "title": "世界总览", "content": "群岛与雾"}
    body.update(overrides)
    return body


# ------------------------------------------------------------------ metadata
def test_layers_and_modes_are_described(client):
    res = client.get("/api/v1/knowledge/layers")
    assert res.status_code == 200
    payload = res.json()
    assert [layer["id"] for layer in payload["layers"]] == [
        "global",
        "story",
        "scene",
        "character",
        "event",
    ]
    assert {mode["id"] for mode in payload["modes"]} == {"storyline", "free"}


# ------------------------------------------------------------ knowledge CRUD
def test_knowledge_crud(client, scenario_id):
    res = client.post(f"/api/v1/scenarios/{scenario_id}/knowledge", json=_entry())
    assert res.status_code == 201, res.text
    entry = res.json()
    assert entry["scenario_id"] == scenario_id
    assert entry["depth"] == 5

    res = client.get(f"/api/v1/scenarios/{scenario_id}/knowledge")
    assert res.status_code == 200
    assert [item["id"] for item in res.json()] == [entry["id"]]

    res = client.post(
        f"/api/v1/scenarios/{scenario_id}/knowledge",
        json=_entry(layer="event", title="小事件", always_on=True, enabled=False),
    )
    assert res.status_code == 201
    disabled = res.json()

    res = client.get(
        f"/api/v1/scenarios/{scenario_id}/knowledge?include_disabled=false"
    )
    assert [item["id"] for item in res.json()] == [entry["id"]]

    res = client.patch(f"/api/v1/knowledge/{entry['id']}", json={"depth": 2})
    assert res.status_code == 200
    assert res.json()["depth"] == 2

    res = client.delete(f"/api/v1/knowledge/{disabled['id']}")
    assert res.status_code == 200
    assert res.json() == {"deleted": True, "id": disabled["id"]}
    assert client.get(f"/api/v1/scenarios/{scenario_id}/knowledge").json() != []


def test_unknown_scenario_and_entry_are_404(client):
    assert client.get("/api/v1/scenarios/ghost/knowledge").status_code == 404
    assert (
        client.post("/api/v1/scenarios/ghost/knowledge", json=_entry()).status_code == 404
    )
    assert client.patch("/api/v1/knowledge/ghost", json={"depth": 2}).status_code == 404
    assert client.delete("/api/v1/knowledge/ghost").status_code == 404


def test_invalid_knowledge_payload_is_rejected(client, scenario_id):
    res = client.post(
        f"/api/v1/scenarios/{scenario_id}/knowledge", json=_entry(layer="unknown-layer")
    )
    assert res.status_code == 422
    res = client.post(
        f"/api/v1/scenarios/{scenario_id}/knowledge", json=_entry(depth=99)
    )
    assert res.status_code == 422
    res = client.post(
        f"/api/v1/scenarios/{scenario_id}/knowledge", json=_entry(script="rm -rf /")
    )
    assert res.status_code == 422  # extra fields are forbidden, never executed


# --------------------------------------------------------------- play config
def test_play_config_defaults_to_free(client, scenario_id):
    res = client.get(f"/api/v1/scenarios/{scenario_id}/play-config")
    assert res.status_code == 200
    assert res.json()["mode"] == "free"
    assert res.json()["max_depth"] == 9


def test_storyline_mode_requires_nodes(client, scenario_id):
    res = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config", json={"mode": "storyline"}
    )
    assert res.status_code == 400
    assert "故事线模式至少需要一个故事线节点" in res.json()["detail"]


def test_storyline_config_roundtrip(client, scenario_id):
    body = {
        "mode": "storyline",
        "max_depth": 4,
        "model_preset_id": "builtin-fast",
        "storyline": {
            "name": "主线",
            "nodes": [
                {"id": "arrive", "title": "抵达"},
                {"id": "open", "title": "开门", "prereq": ["arrive"]},
            ],
        },
    }
    res = client.put(f"/api/v1/scenarios/{scenario_id}/play-config", json=body)
    assert res.status_code == 200, res.text
    stored = res.json()
    assert stored["mode"] == "storyline"
    assert [node["id"] for node in stored["storyline"]["nodes"]] == ["arrive", "open"]

    res = client.post(f"/api/v1/scenarios/{scenario_id}/knowledge/validate")
    assert res.json() == {"valid": True, "errors": []}


def test_unknown_model_preset_is_rejected(client, scenario_id):
    res = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config",
        json={"mode": "free", "model_preset_id": "ghost"},
    )
    assert res.status_code == 400
    assert "unknown model preset 'ghost'" in res.json()["detail"]


def test_validation_reports_broken_storyline(client, scenario_id):
    body = {
        "mode": "storyline",
        "storyline": {
            "name": "坏线",
            "nodes": [{"id": "a", "title": "一", "prereq": ["ghost"]}],
        },
    }
    res = client.put(f"/api/v1/scenarios/{scenario_id}/play-config", json=body)
    assert res.status_code == 400
    assert any("ghost" in error for error in res.json()["detail"])


# ----------------------------------------------------------- context preview
def test_context_preview_applies_depth_and_mode_filters(client, scenario_id):
    client.post(f"/api/v1/scenarios/{scenario_id}/knowledge", json=_entry(title="浅层", depth=1))
    client.post(
        f"/api/v1/scenarios/{scenario_id}/knowledge", json=_entry(title="深层", depth=8)
    )
    client.post(
        f"/api/v1/scenarios/{scenario_id}/knowledge",
        json=_entry(layer="story", title="故事线专属", mode_scope="storyline"),
    )

    res = client.get(
        f"/api/v1/scenarios/{scenario_id}/context/preview?mode=free&max_depth=3"
    )
    assert res.status_code == 200
    payload = res.json()
    assert [block["title"] for block in payload["blocks"]] == ["浅层"]
    reasons = {item["title"]: item["reason"] for item in payload["stats"]["excluded"]}
    assert "depth 8 > 3" in reasons["深层"]
    assert "自由模式" in reasons["故事线专属"]
    assert payload["stats"]["max_depth"] == 3


def test_context_preview_supports_all_entered_nodes_and_ignores_them_in_free_mode(client, scenario_id):
    entries = {}
    for index, name in enumerate(["global", "main", "branch", "side"]):
        created = client.post(
            f"/api/v1/scenarios/{scenario_id}/knowledge",
            json=_entry(title=name, layer="global" if name == "global" else "character", sort_order=index),
        )
        assert created.status_code == 201, created.text
        entries[name] = created.json()["id"]
    saved = client.put(f"/api/v1/scenarios/{scenario_id}/play-config", json={
        "mode": "storyline",
        "storyline": {"nodes": [
            {"id": "arrive", "title": "Main", "knowledge_ids": [entries["main"]]},
            {"id": "branch", "title": "Branch", "kind": "branch", "prereq": ["arrive"],
             "knowledge_ids": [entries["branch"]]},
            {"id": "side", "title": "Side", "kind": "side", "requires_event": "keeper_alert",
             "knowledge_ids": [entries["side"]]},
        ]},
    })
    assert saved.status_code == 200, saved.text
    url = f"/api/v1/scenarios/{scenario_id}/context/preview"
    params = {"story_node": "arrive", "story_nodes": " arrive, , branch, side, "}
    res = client.get(url, params=params)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["scenario_id"] == scenario_id
    assert body["mode"] == "storyline"
    assert body["story_node"] == "arrive"
    assert body["story_nodes"] == ["arrive", "branch", "side"]
    assert [block["entry_id"] for block in body["blocks"]] == [
        entries[name] for name in ["main", "branch", "side", "global"]
    ]
    assert body["stats"]["included"] == 4

    legacy = client.get(url, params={"story_node": "arrive"}).json()
    assert legacy["story_nodes"] == ["arrive"]
    assert [block["entry_id"] for block in legacy["blocks"]] == [
        entries[name] for name in ["main", "global", "branch", "side"]
    ]
    free = client.get(url, params=dict(params, mode="free"))
    assert free.status_code == 200, free.text
    assert free.json() == client.get(url, params={"mode": "free"}).json()
    assert free.json()["story_node"] is None
    assert free.json()["story_nodes"] == []
    assert [block["entry_id"] for block in free.json()["blocks"]] == [
        entries[name] for name in ["global", "main", "branch", "side"]
    ]


# ------------------------------------------------------------- model presets
def test_model_preset_lifecycle(client):
    res = client.get("/api/v1/model-presets")
    assert res.status_code == 200
    builtins = res.json()
    assert {preset["id"] for preset in builtins} == {
        "builtin-balanced",
        "builtin-grim",
        "builtin-fast",
        "builtin-intent",
    }

    res = client.post(
        "/api/v1/model-presets",
        json={"name": "实验叙事", "role": "narrator", "temperature": 1.2, "max_tokens": 640},
    )
    assert res.status_code == 201
    preset = res.json()
    assert preset["is_builtin"] is False

    res = client.patch(f"/api/v1/model-presets/{preset['id']}", json={"max_tokens": 400})
    assert res.json()["max_tokens"] == 400

    assert client.delete(f"/api/v1/model-presets/{preset['id']}").status_code == 200
    assert client.patch("/api/v1/model-presets/ghost", json={"max_tokens": 1}).status_code == 404


def test_builtin_preset_cannot_be_deleted(client):
    res = client.delete("/api/v1/model-presets/builtin-balanced")
    assert res.status_code == 409
    assert "builtin" in res.json()["detail"]


def test_context_preview_uses_budget_and_focus(client, scenario_id):
    client.post(
        f"/api/v1/scenarios/{scenario_id}/knowledge",
        json=_entry(title="Tavern", content="x" * 40, tags=["tavern"]),
    )
    client.post(
        f"/api/v1/scenarios/{scenario_id}/knowledge",
        json=_entry(title="Distant", content="y" * 40, tags=["harbor"]),
    )
    res = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config",
        json={
            "mode": "free",
            "context_budget_chars": 50,
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["context_budget_chars"] == 50

    preview = client.get(
        f"/api/v1/scenarios/{scenario_id}/context/preview",
        params={"mode": "free", "focus": "tavern"},
    ).json()
    assert [block["title"] for block in preview["blocks"]] == ["Tavern"]
    assert any("预算" in item["reason"] for item in preview["stats"]["excluded"])
