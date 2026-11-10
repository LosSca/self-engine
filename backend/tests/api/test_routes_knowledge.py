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
def test_new_scenarios_start_with_a_valid_starter_storyline(client, scenario_id):
    res = client.get(f"/api/v1/scenarios/{scenario_id}/play-config")
    assert res.status_code == 200
    config = res.json()
    assert config["mode"] == "storyline"
    assert config["max_depth"] == 9
    story = config["storyline"]
    assert [line["id"] for line in story["lines"]] == ["trunk"]
    assert [node["id"] for node in story["nodes"]] == ["opening", "first_turn"]
    # The template is ordinary editable data: it validates and nothing is locked.
    assert story["lines"][0]["locked"] is False
    assert all(node["locked"] is False for node in story["nodes"])
    res = client.post(f"/api/v1/scenarios/{scenario_id}/knowledge/validate")
    assert res.json() == {"valid": True, "errors": []}


def test_starter_storyline_can_be_replaced_with_free_mode(client, scenario_id):
    res = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config", json={"mode": "free"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["mode"] == "free"


def test_storyline_mode_rejects_an_empty_storyline(client, scenario_id):
    res = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config",
        json={"mode": "storyline", "storyline": {"name": "空", "nodes": []}},
    )
    assert res.status_code == 400
    assert "故事线模式至少需要一个故事线节点" in res.json()["detail"]


def test_storyline_mode_without_a_storyline_keeps_the_starter(client, scenario_id):
    res = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config", json={"mode": "storyline"}
    )
    assert res.status_code == 200, res.text
    assert [node["id"] for node in res.json()["storyline"]["nodes"]] == [
        "opening",
        "first_turn",
    ]


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


def test_storyline_requires_event_must_exist_when_a_world_is_bound(client, scenario_id):
    import os

    from app.db.game_state_store import GameStateStore
    from app.services.world_service import save_world
    from tests.support import village_world

    world = village_world().model_copy(update={"scenario_id": scenario_id})
    store = GameStateStore(os.environ["GAME_STATE_DB_PATH"])
    try:
        save_world(store, world)
    finally:
        store.close()

    def body(event_id):
        return {
            "mode": "storyline",
            "storyline": {"nodes": [
                {"id": "main", "title": "Main"},
                {
                    "id": "side", "title": "Side", "kind": "side",
                    "requires_event": event_id,
                },
            ]},
        }

    rejected = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config", json=body("ghost")
    )
    assert rejected.status_code == 400
    assert any("ghost" in error for error in rejected.json()["detail"])
    accepted = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config",
        json=body(world.events[0].id),
    )
    assert accepted.status_code == 200, accepted.text


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


def test_intent_preset_and_narrator_prompt_roundtrip(client, scenario_id):
    body = {
        "mode": "free",
        "intent_preset_id": "builtin-fast",
        "narrator_prompt": "Use terse, cold prose.",
    }
    res = client.put(f"/api/v1/scenarios/{scenario_id}/play-config", json=body)
    assert res.status_code == 200, res.text
    stored = res.json()
    assert stored["intent_preset_id"] == "builtin-fast"
    assert stored["narrator_prompt"] == "Use terse, cold prose."

    bad = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config",
        json={"mode": "free", "intent_preset_id": "ghost"},
    )
    assert bad.status_code == 400
    assert "ghost" in bad.text


def test_ai_knowledge_generation_requires_a_model(client, scenario_id, monkeypatch):
    from app.services import ai_generator

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: None)
    res = client.post(
        f"/api/v1/scenarios/{scenario_id}/generate/knowledge",
        json={"prompt": "海岛", "count": 1},
    )
    assert res.status_code == 503


def test_ai_knowledge_generation_returns_editable_drafts(client, scenario_id, monkeypatch):
    import json

    from app.services import ai_generator

    class Router:
        async def complete(self, messages, **kwargs):
            return json.dumps({"entries": [
                {"layer": "global", "title": "群岛", "content": "雾", "tags": ["world"]}
            ]})

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: Router())
    res = client.post(
        f"/api/v1/scenarios/{scenario_id}/generate/knowledge",
        json={"prompt": "海岛", "count": 1},
    )
    assert res.status_code == 200, res.text
    assert res.json()["entries"][0]["title"] == "群岛"
    # Drafts are not saved until the author applies them.
    assert client.get(f"/api/v1/scenarios/{scenario_id}/knowledge").json() == []


# ------------------------------------------------- armed event packs (preview)
def _bind_scenario_world(scenario_id, campaign_id):
    """Persist the demo village for this scenario and bind one campaign to it."""
    import os

    from app.db.game_state_store import GameStateStore
    from app.services.world_service import bind_world, save_world
    from tests.support import village_world

    world = village_world().model_copy(update={"scenario_id": scenario_id})
    store = GameStateStore(os.environ["GAME_STATE_DB_PATH"])
    try:
        save_world(store, world)
        state = bind_world(store, campaign_id, world.world_id, reset=True)
    finally:
        store.close()
    return world, state


def _pack_config(event_id):
    return {
        "mode": "storyline",
        "storyline": {
            "nodes": [{"id": "arrive", "title": "抵达"}],
            "packs": [
                {
                    "id": "pack_edge",
                    "title": "森林的传闻",
                    "event_ids": [event_id],
                    "requires": [{"kind": "at_location", "target": "forest_edge"}],
                }
            ],
        },
    }


def test_context_preview_arms_packs_from_the_campaign_state(client, scenario_id):
    from app.db.game_state_store import GameStateStore
    from app.services.action_service import ActionService
    from app.models.game import ActionRequest
    import asyncio
    import os

    world, _state = _bind_scenario_world(scenario_id, "camp-packs")
    event_id = world.events[0].id
    saved = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config", json=_pack_config(event_id)
    )
    assert saved.status_code == 200, saved.text
    url = f"/api/v1/scenarios/{scenario_id}/context/preview"

    before = client.get(url, params={"campaign_id": "camp-packs"})
    assert before.status_code == 200, before.text
    assert before.json()["armed_packs"] == []

    store = GameStateStore(os.environ["GAME_STATE_DB_PATH"])
    try:
        service = ActionService(store)
        asyncio.run(
            service.execute(
                ActionRequest(
                    campaign_id="camp-packs",
                    request_id="req-move-edge",
                    action="move",
                    target_id="forest_edge",
                )
            )
        )
    finally:
        store.close()

    after = client.get(url, params={"campaign_id": "camp-packs"}).json()
    assert after["armed_packs"] == ["pack_edge"]

    # Without a campaign the preview shows the authored picture, never a guess.
    guest = client.get(url).json()
    assert guest["armed_packs"] == []
    # An unknown campaign is reported as empty rather than 500.
    unknown = client.get(url, params={"campaign_id": "ghost"})
    assert unknown.status_code == 200
    assert unknown.json()["armed_packs"] == []


def test_locked_storyline_content_cannot_be_rewritten(client, scenario_id):
    body = {
        "mode": "storyline",
        "storyline": {
            "name": "主线",
            "lines": [{"id": "line_main", "title": "主线"}],
            "nodes": [
                {"id": "arrive", "title": "抵达", "line": "line_main"},
                {"id": "open", "title": "开门", "line": "line_main", "prereq": ["arrive"]},
            ],
        },
    }
    assert (
        client.put(f"/api/v1/scenarios/{scenario_id}/play-config", json=body).status_code
        == 200
    )

    unlocked = {
        "mode": "storyline",
        "storyline": {
            **body["storyline"],
            "lines": [{"id": "line_main", "title": "主线", "locked": True}],
            "nodes": [
                {"id": "arrive", "title": "抵达", "line": "line_main", "locked": True},
                {"id": "open", "title": "开门", "line": "line_main", "prereq": ["arrive"]},
            ],
        },
    }
    res = client.put(f"/api/v1/scenarios/{scenario_id}/play-config", json=unlocked)
    assert res.status_code == 200, res.text

    tampered = {
        "mode": "storyline",
        "storyline": {
            **body["storyline"],
            "lines": [{"id": "line_main", "title": "主线", "locked": True}],
            "nodes": [
                {"id": "arrive", "title": "换了标题", "line": "line_main", "locked": True},
                {"id": "open", "title": "开门", "line": "line_main", "prereq": ["arrive"]},
            ],
        },
    }
    res = client.put(f"/api/v1/scenarios/{scenario_id}/play-config", json=tampered)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert any("arrive" in error and "锁定" in error for error in detail)

    # An unrelated field may still change while the settled content stays put.
    res = client.put(
        f"/api/v1/scenarios/{scenario_id}/play-config",
        json={"mode": "storyline", "max_depth": 3},
    )
    assert res.status_code == 200, res.text
    assert res.json()["max_depth"] == 3
