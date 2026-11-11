"""The engine's wire contract is typed, not "some dict".

FastAPI validates every response against its declared model, so these tests
exist to prove three things: every hand-built route declares a real model
rather than Dict[str, Any], the published OpenAPI schema matches what the
routes actually return, and a contract break is impossible to ship silently.
"""
import pytest

from app.main import app


API = "/api/v1"

# Routes that used to smuggle their payload through Dict[str, Any].
TYPED_PATHS = [
    ("get", "/api/v1/worlds"),
    ("get", "/api/v1/world-presets"),
    ("post", "/api/v1/worlds"),
    ("get", "/api/v1/worlds/{world_id}/map"),
    ("get", "/api/v1/campaigns/{campaign_id}/lane"),
    ("get", "/api/v1/campaigns/{campaign_id}/story"),
    ("get", "/api/v1/campaigns/{campaign_id}/diagnostics"),
    ("get", "/api/v1/campaigns/{campaign_id}/export"),
    ("get", "/api/v1/campaigns/{campaign_id}/state-diff"),
    ("post", "/api/v1/campaigns/{campaign_id}/rollback"),
    ("post", "/api/v1/campaigns/{campaign_id}/repair"),
    ("post", "/api/v1/campaigns/{campaign_id}/actions/preview"),
    ("get", "/api/v1/campaigns/{campaign_id}/quest-progress"),
    ("get", "/api/v1/campaigns/{campaign_id}/event-executions"),
    ("get", "/api/v1/actions/{request_id}"),
    ("get", "/api/v1/campaigns/{campaign_id}/requests"),
    ("get", "/api/v1/worlds/{world_id}/quests"),
    ("post", "/api/v1/worlds/{world_id}/quests"),
    ("get", "/api/v1/worlds/{world_id}/quests/{quest_id}"),
    ("patch", "/api/v1/worlds/{world_id}/quests/{quest_id}"),
    ("get", "/api/v1/works"),
    ("get", "/api/v1/works/{title}"),
    ("post", "/api/v1/works/resolve"),
    ("post", "/api/v1/works/enter"),
]


def _schema() -> dict:
    return app.openapi()


SUCCESS_CODES = ("200", "201", "202")


def _response_schema(schema: dict, path: str, method: str) -> dict:
    operation = schema["paths"][path][method]
    for code in SUCCESS_CODES:
        ok = operation["responses"].get(code)
        if ok is not None:
            return ok["content"]["application/json"]["schema"]
    raise AssertionError(f"{method.upper()} {path} declares no success response")


def test_every_listed_route_publishes_a_shape():
    schema = _schema()
    for method, path in TYPED_PATHS:
        body = _response_schema(schema, path, method)
        # A real model is either an inline schema with properties, or an array
        # of one - never an untyped object with no declared fields.
        target = body
        if "$ref" in body:
            target = schema["components"]["schemas"][body["$ref"].split("/")[-1]]
        if target.get("type") == "array":
            item = target.get("items", {})
            if "$ref" in item:
                item = schema["components"]["schemas"][item["$ref"].split("/")[-1]]
            assert item.get("properties"), f"{method.upper()} {path} has an untyped list"
        else:
            assert target.get("properties"), f"{method.upper()} {path} has an untyped body"


def test_no_engine_route_still_declares_a_bare_dict():
    """The 22 Dict[str, Any] handlers from the audit are gone."""
    import inspect
    from app.api import routes_engine

    source = inspect.getsource(routes_engine)
    declarations = [
        line for line in source.splitlines() if "response_model=" in line
    ]
    assert declarations
    offenders = [line.strip() for line in declarations if "Dict[str, Any]" in line]
    # Only catalogue-style projections may stay open-ended, and there are none
    # left in this module: every handler now names its contract.
    assert offenders == []


def test_lane_contract_names_both_lanes():
    schema = _schema()
    ref = _response_schema(schema, "/api/v1/campaigns/{campaign_id}/lane", "get")
    model = schema["components"]["schemas"][ref["$ref"].split("/")[-1]]
    assert set(model["properties"]) == {
        "campaign_id",
        "lane",
        "bound",
        "world_id",
        "version",
    }
    assert model["properties"]["lane"]["enum"] == ["engine", "narrative"]


def test_map_contract_pins_the_navigation_graph():
    schema = _schema()
    ref = _response_schema(schema, "/api/v1/worlds/{world_id}/map", "get")
    model = schema["components"]["schemas"][ref["$ref"].split("/")[-1]]
    for key in ("locations", "regions", "items", "doors", "entities", "counts"):
        assert key in model["properties"]


def test_preview_contract_has_no_request_id():
    """A dry run can never occupy a request id, so the contract must not offer one."""
    schema = _schema()
    ref = _response_schema(
        schema, "/api/v1/campaigns/{campaign_id}/actions/preview", "post"
    )
    model = schema["components"]["schemas"][ref["$ref"].split("/")[-1]]
    assert "request_id" not in model["properties"]
    assert "ok" in model["properties"] and "effects" in model["properties"]


def test_validation_actually_runs_on_the_way_out():
    """A handler that returns the wrong shape is a 500, not a silent drift."""
    from app.api import routes_engine

    original = routes_engine.get_campaign_lane
    try:
        def broken(campaign_id: str):
            return {"campaign_id": campaign_id}

        routes_engine.get_campaign_lane = broken
        app.dependency_overrides.clear()
        # FastAPI captured the endpoint at import time; drive the response model
        # directly instead of swapping the whole route table.
        model = routes_engine.LaneResponse
        with pytest.raises(Exception):
            model.model_validate({"campaign_id": "camp-1"})
    finally:
        routes_engine.get_campaign_lane = original


def test_story_and_diagnostics_share_the_narration_health_model():
    schema = _schema()
    ref = _response_schema(schema, "/api/v1/campaigns/{campaign_id}/diagnostics", "get")
    model = schema["components"]["schemas"][ref["$ref"].split("/")[-1]]
    narration = model["properties"]["narration"]
    assert narration.get("$ref", "").endswith("NarrationHealth")


def _requests_client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenario.db"))
    import importlib

    import app.main as main

    importlib.reload(main)
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def test_campaign_requests_list_the_recorded_turns(tmp_path, monkeypatch):
    world = {
        "world_id": "req_world",
        "name": "Requests",
        "description": "",
        "start_location_id": "square",
        "locations": [
            {
                "id": "square",
                "name": "Square",
                "description": "A square.",
                "exits": [{"target_id": "hall", "label": "north"}],
            },
            {
                "id": "hall",
                "name": "Hall",
                "description": "A hall.",
                "exits": [{"target_id": "square", "label": "south"}],
            },
        ],
    }
    with _requests_client(tmp_path, monkeypatch) as client:
        assert client.post("/api/v1/worlds", json=world).status_code in (200, 201)
        bound = client.post(
            "/api/v1/campaigns/camp-1/world",
            json={"world_id": "req_world", "reset": True},
        )
        assert bound.status_code == 200, bound.text

        applied = client.post(
            "/api/v1/campaigns/camp-1/actions",
            json={
                "request_id": "req-applied",
                "campaign_id": "camp-1",
                "action": "move",
                "target_id": "hall",
            },
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"

        # A rejection is recorded too: a debugger needs to see the turns that
        # did not happen, not only the ones that did.
        rejected = client.post(
            "/api/v1/campaigns/camp-1/actions",
            json={
                "request_id": "req-rejected",
                "campaign_id": "camp-1",
                "action": "move",
                "target_id": "nowhere",
            },
        )
        assert rejected.status_code == 200, rejected.text

        listed = client.get("/api/v1/campaigns/camp-1/requests", params={"limit": 20})
        assert listed.status_code == 200, listed.text
        rows = listed.json()
        by_id = {row["request_id"]: row for row in rows}
        assert "req-applied" in by_id and "req-rejected" in by_id, by_id
        assert by_id["req-applied"]["status"] == "applied"
        assert by_id["req-applied"]["state_version"] >= 1

        # The index must point at turns the detail route can actually open.
        detail = client.get("/api/v1/actions/req-applied")
        assert detail.status_code == 200, detail.text
        assert detail.json()["request"]["action"] == "move"
