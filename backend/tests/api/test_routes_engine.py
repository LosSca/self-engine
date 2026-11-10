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
    # The knowledge graph is a real Neo4j instance on the developer's box; a
    # test must never depend on it being up or reachable.
    monkeypatch.setenv("ENGINE_GRAPH_MIRROR", "0")
    # Never let an HTTP test depend on a locally running model endpoint.
    monkeypatch.setenv("ENGINE_NARRATOR", "template")
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
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
    assert body["storyline"] == {"name": "", "nodes": [], "lines": [], "packs": []}
    assert body["armed_packs"] == [] and body["fired_packs"] == []


def test_story_endpoint_exposes_line_metadata_without_mutating_progress(client):
    created = client.post("/api/scenarios/", json={"title": "Story lines", "language": "zh"})
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]
    lines = [
        {"id": "trunk", "title": "Main line", "kind": "main", "description": "The trunk", "enabled": True},
        {"id": "detour", "title": "Branch line", "kind": "branch", "description": "A detour", "enabled": True},
        {"id": "independent", "title": "Side line", "kind": "side", "description": "On hold", "enabled": False},
    ]
    nodes = [
        {"id": "arrive", "title": "Arrival", "kind": "main", "line": "trunk", "summary": "Arrive at the village"},
        {"id": "branch", "title": "Detour", "kind": "branch", "line": "detour", "summary": "Look around",
         "prereq": ["arrive"]},
        {"id": "side", "title": "Independent", "kind": "side", "line": "independent", "summary": "A side story",
         "conditions": [{"kind": "at_location", "target": "village_square"}]},
        {"id": "open", "title": "Open warehouse", "kind": "main", "summary": "An ungrouped legacy node",
         "prereq": ["arrive"], "conditions": [{"kind": "door_open", "target": "warehouse_door"}]},
    ]
    config_url = f"{API}/scenarios/{scenario_id}/play-config"
    saved = client.put(config_url, json={
        "mode": "storyline", "storyline": {"name": "Warehouse", "lines": lines, "nodes": nodes},
    })
    assert saved.status_code == 200, saved.text
    stored = client.get(config_url).json()["storyline"]
    assert stored["lines"] == [{**line, "locked": False} for line in lines]
    assert [node["line"] for node in stored["nodes"]] == ["trunk", "detour", "independent", ""]

    payload = village_world_data()
    payload["scenario_id"] = scenario_id
    assert _create_world(client, payload).status_code == 200
    assert client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"}).status_code == 200
    before = client.get(f"{API}/campaigns/camp-1/state").json()
    response = client.get(f"{API}/campaigns/camp-1/story")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == "camp-1"
    assert body["mode"] == "storyline"
    assert body["entered"] == ["arrive", "branch"]
    assert body["active"] == "arrive"
    assert body["pending"] == ["side", "open"]
    assert body["locked"] == []
    assert "已停用" in body["reasons"]["side"]
    assert body["storyline"] == {
        "name": "Warehouse", "lines": stored["lines"],
        "nodes": [
            {key: node.get(key, "") for key in ["id", "title", "kind", "line", "summary"]}
            | {"requires_event_pack": None, "locked": node.get("locked", False)}
            for node in nodes
        ],
        "packs": [],
    }
    assert client.get(f"{API}/campaigns/camp-1/state").json() == before

    assert client.put(config_url, json={"mode": "free"}).status_code == 200
    free = client.get(f"{API}/campaigns/camp-1/story").json()
    assert free["mode"] == "free"
    assert free["storyline"] == {"name": "", "nodes": [], "lines": [], "packs": []}
    assert client.get(config_url).json()["storyline"] == stored


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


def test_diagnostics_reports_ok_narration_before_any_turn(client):
    _create_world(client)
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})
    body = client.get(f"{API}/campaigns/camp-1/diagnostics").json()
    assert body["narration"]["status"] == "unreported"
    assert body["narration"]["recent_failures"] == 0
    assert body["narration"]["narrator"]["choice"] in {"template", "llm"}


def test_diagnostics_sees_a_broken_narrator(client, monkeypatch):
    """A narrator that fails every turn must be visible from /diagnostics."""
    monkeypatch.setenv("ENGINE_NARRATOR", "template")
    _create_world(client)
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})

    from app.api import routes_engine

    broken = routes_engine._service()
    try:
        class Boom:
            async def narrate(self, *args, **kwargs):
                raise TimeoutError("model call timed out")

        broken.narrator = Boom()
        import asyncio

        from app.models.game import ActionRequest

        result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            broken.execute(
                ActionRequest(
                    campaign_id="camp-1", request_id="diag-1", action="look"
                )
            )
        )
        assert result.status == "applied"
        assert result.narrative is not None
        assert result.narrative.status == "failed"
    finally:
        broken.store.close()

    body = client.get(f"{API}/campaigns/camp-1/diagnostics").json()
    assert body["narration"]["status"] == "degraded"
    assert body["narration"]["recent_failures"] >= 1
    assert "TimeoutError" in body["narration"]["last_failure"]["payload"]["error"]


def test_snapshot_listing_reports_bytes(client):
    _create_world(client)
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})
    client.post(
        f"{API}/campaigns/camp-1/actions",
        json={"campaign_id": "camp-1", "request_id": "r1", "action": "look"},
    )
    rows = client.get(f"{API}/campaigns/camp-1/snapshots").json()
    assert rows and all(row["bytes"] > 0 for row in rows)
    latest = rows[0]
    assert latest["stored_bytes"] >= latest["bytes"]
    pruned = client.get(f"{API}/campaigns/camp-1/snapshots/prune").json()
    assert pruned["bytes"] > 0


def test_committed_narration_is_recorded_in_the_event_log(client):
    _create_world(client)
    client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})
    result = client.post(
        f"{API}/campaigns/camp-1/actions",
        json={"campaign_id": "camp-1", "request_id": "r1", "action": "look"},
    ).json()
    assert result["narrative"]["status"] == "ok"
    events = client.get(f"{API}/campaigns/camp-1/events").json()
    types = [event["event_type"] for event in events]
    assert "narrator.response" in types, types
    stored = next(e for e in events if e["event_type"] == "narrator.response")
    assert stored["payload"]["text"] == result["narrative"]["text"]


def test_graph_mirror_is_off_without_neo4j_credentials(client, monkeypatch):
    from app.api import routes_engine

    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    assert routes_engine._graph_mirror_sink() is None
    monkeypatch.setattr(
        "app.engines.graphiti_engine.GraphitiEngine.__init__",
        lambda self, *a, **k: None,
    )
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("ENGINE_GRAPH_MIRROR", "0")
    sink = routes_engine._graph_mirror_sink()
    calls = []
    monkeypatch.setattr(
        "app.engines.graphiti_engine.GraphitiEngine.ingest_episode",
        lambda *a, **k: calls.append((a, k)),
    )
    sink("camp-1", None, type("N", (), {"text": "prose"})())
    import time

    time.sleep(0.05)
    assert calls == [], "ENGINE_GRAPH_MIRROR=0 must silence the mirror"


def test_campaign_export_and_import_roundtrip(client):
    assert _create_world(client).status_code == 200
    bound = client.post(
        f"{API}/campaigns/camp-export/world",
        json={"world_id": "demo_village", "reset": True},
    )
    assert bound.status_code == 200
    taken = client.post(
        f"{API}/campaigns/camp-export/text-actions",
        json={"text": "take iron_key", "request_id": "exp-1", "narrate": False},
    ).json()
    assert taken["status"] == "applied"

    exported = client.get(f"{API}/campaigns/camp-export/export").json()
    assert exported["format"] == "lunar-campaign"
    assert exported["state"]["inventory"]["iron_key"] == 1
    assert exported["state"]["campaign_id"] == "camp-export"
    assert exported["events"]

    # A different campaign can be created from the file; the URL id wins.
    payload = dict(exported["state"], campaign_id="ignored-in-file")
    restored = client.post(
        f"{API}/campaigns/camp-restored/import",
        json={"state": payload, "reset": True},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["campaign_id"] == "camp-restored"
    assert restored.json()["inventory"]["iron_key"] == 1

    # Overwriting needs an explicit reset.
    assert client.post(
        f"{API}/campaigns/camp-restored/import",
        json={"state": payload},
    ).status_code == 409
    unknown_world = dict(payload, world_id="ghost")
    assert client.post(
        f"{API}/campaigns/camp-ghost/import",
        json={"state": unknown_world, "reset": True},
    ).status_code == 400


def test_ai_world_generation_saves_a_validated_world(client, monkeypatch):
    import json

    from app.services import ai_generator

    payload = {
        "name": "AI 小岛", "start_location_id": "a",
        "locations": [{"id": "a", "name": "A", "region": "island", "x": 0, "y": 0}],
        "items": [], "doors": [], "entities": [], "quests": [], "events": [],
    }

    class Router:
        async def complete(self, messages, **kwargs):
            return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: Router())
    res = client.post(
        f"{API}/worlds/generate",
        json={"scenario_id": "", "prompt": "小岛"},
    )
    assert res.status_code == 200, res.text
    world_id = res.json()["world_id"]
    assert world_id.startswith("ai_")
    fetched = client.get(f"{API}/worlds/{world_id}")
    assert fetched.status_code == 200
    assert fetched.json()["locations"][0]["region"] == "island"


def test_ai_world_generation_without_model_is_503(client, monkeypatch):
    from app.services import ai_generator

    monkeypatch.setattr(ai_generator, "router_for", lambda preset=None: None)
    res = client.post(f"{API}/worlds/generate", json={"prompt": "小岛"})
    assert res.status_code == 503


def test_story_endpoint_reports_armed_and_fired_packs(client):
    created = client.post("/api/scenarios/", json={"title": "Packs", "language": "zh"})
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]
    payload = village_world_data()
    payload["scenario_id"] = scenario_id
    assert _create_world(client, payload).status_code == 200
    world = client.get(f"{API}/worlds/demo_village").json()
    event_id = world["events"][0]["id"]
    packs = [
        {
            "id": "pack_edge",
            "title": "Forest rumour",
            "event_ids": [event_id],
            "requires": [{"kind": "at_location", "target": "forest_edge"}],
            "enabled": True,
            "once": True,
        }
    ]
    config_url = f"{API}/scenarios/{scenario_id}/play-config"
    saved = client.put(config_url, json={
        "mode": "storyline",
        "storyline": {
            "name": "With packs",
            "nodes": [
                {"id": "arrive", "title": "Arrival"},
                {"id": "rumour", "title": "Rumour", "kind": "side", "requires_event_pack": "pack_edge"},
            ],
            "packs": packs,
        },
    })
    assert saved.status_code == 200, saved.text
    assert client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"}).status_code == 200

    body = client.get(f"{API}/campaigns/camp-1/story").json()
    assert body["armed_packs"] == []
    assert body["fired_packs"] == []
    stored = body["storyline"]["packs"]
    assert [pack["id"] for pack in stored] == ["pack_edge"]
    assert stored[0]["event_ids"] == [event_id]
    assert stored[0]["requires"][0]["kind"] == "at_location"
    assert stored[0]["requires"][0]["target"] == "forest_edge"
    assert stored[0]["once"] is True
    assert body["storyline"]["nodes"][1]["requires_event_pack"] == "pack_edge"
    assert body["pending"] == ["rumour"]
    assert "pack_edge" in body["reasons"]["rumour"]

    moved = client.post(f"{API}/campaigns/camp-1/actions", json={
        "campaign_id": "camp-1",
        "request_id": "req-move-edge",
        "action": "move",
        "target_id": "forest_edge",
        "narrate": False,
    })
    assert moved.status_code == 200, moved.text
    after = client.get(f"{API}/campaigns/camp-1/story").json()
    assert after["armed_packs"] == ["pack_edge"]
    assert after["fired_packs"] == []
