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


# ---------------------------------------------------------------- sunset gate
def _bound_campaign(client, scenario_title: str = "Sunset") -> tuple[dict, dict]:
    """A campaign bound to a validated world, ready for the rules lane."""
    scenario = client.post(
        "/api/scenarios/",
        json={"title": scenario_title, "language": "zh", "opening_narrative": OPENING},
    ).json()
    world = {
        "world_id": f"world_{scenario['id'][:8]}",
        "scenario_id": scenario["id"],
        "name": "Sunset world",
        "start_location_id": "square",
        "locations": [{"id": "square", "name": "Square", "item_ids": ["stone"]}],
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
        f"/api/v1/campaigns/{campaign['id']}/world", json={"world_id": world["world_id"]}
    ).status_code == 200
    return scenario, campaign


def test_action_without_a_bindable_world_is_reported_not_silently_downgraded(client):
    """The silent legacy fallback is gone: an unbindable campaign gets a 409.

    Falling through to the retired session used to mean the same utterance was
    judged by two different fact models depending on whether a world happened to
    be bound. The endpoint now says so, and offers `compat_mode` explicitly.
    """
    with patch("app.api.routes_game._unified_service") as unified:
        from app.api.routes_game import RulesLaneUnavailable

        unified.side_effect = RulesLaneUnavailable("no world for this campaign")
        response = client.post(
            "/api/game/action",
            json={"campaign_id": "orphan", "action": "look around"},
        )
    assert response.status_code == 409
    assert "no world for this campaign" in response.json()["detail"]


def test_history_and_journal_read_the_engine_without_a_legacy_session(client):
    """Panels are projections of authoritative state, not of a live session."""
    _scenario, campaign = _bound_campaign(client)
    client.post(
        f"/api/v1/campaigns/{campaign['id']}/text-actions",
        json={"text": "take stone", "request_id": "sunset-1", "narrate": False},
    )

    history = client.get(f"/api/game/{campaign['id']}/history").json()["messages"]
    # The projection comes from committed requests, so the player's line is
    # first; whether the lane also stored prose is not what this test asserts.
    assert history and history[0]["role"] == "user"
    assert history[0]["content"]
    assert all(message["role"] in ("user", "assistant") for message in history)

    journal = client.get(f"/api/game/{campaign['id']}/journal").json()
    assert isinstance(journal, list)
    assert all(entry["source"] == "rules" for entry in journal)
    assert any(entry["category"] == "location" for entry in journal)

    filtered = client.get(
        f"/api/game/{campaign['id']}/journal?category=story"
    ).json()
    assert all(entry["category"] == "story" for entry in filtered)


def test_rewind_of_a_bound_campaign_still_refuses_to_diverge(client):
    _scenario, campaign = _bound_campaign(client, "Rewind")
    response = client.post(f"/api/game/{campaign['id']}/rewind")
    assert response.status_code == 409
    assert "engine campaigns cannot use legacy rewind" in response.json()["detail"].lower()



def test_npc_mind_panels_rebuild_from_stored_thoughts(client):
    """Minds persist as NPC_THOUGHT events, so a panel read needs no session."""
    from app.api import routes_game

    _scenario, campaign = _bound_campaign(client, "Minds")
    routes_game._sessions.clear()
    routes_game._event_store.upsert_npc_thought(
        campaign["id"],
        "Barkeep",
        {"attitude": "wary of strangers"},
        ["The Barkeep"],
    )

    minds = client.get(f"/api/game/{campaign['id']}/npc-minds").json()
    assert [mind["name"] for mind in minds] == ["Barkeep"]
    assert campaign["id"] not in routes_game._sessions, "a panel read must not build a session"

    characters = client.get(f"/api/game/{campaign['id']}/characters").json()
    assert any(entry["name"] == "Barkeep" for entry in characters)

    client.put(
        f"/api/game/{campaign['id']}/npc-minds/Barkeep",
        json={"thoughts": {"secret": "knows the tunnel"}},
    )
    updated = {m["name"]: m for m in client.get(f"/api/game/{campaign['id']}/npc-minds").json()}
    assert "secret" in updated["Barkeep"]["thoughts"]


def test_memory_crystal_panel_reads_the_shared_engine(client):
    from app.api import routes_game

    _scenario, campaign = _bound_campaign(client, "Crystals")
    routes_game._sessions.clear()
    response = client.get(f"/api/game/{campaign['id']}/memory-crystals")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert campaign["id"] not in routes_game._sessions


def test_graph_search_reduces_the_question_to_known_entities(client):
    """"The graph only matches indexed names, so the sentence is reduced first."""
    _scenario, campaign = _bound_campaign(client, "Graph")
    response = client.get(
        f"/api/game/{campaign['id']}/graph-search", params={"q": "who is the knight"}
    )
    assert response.status_code == 200
    assert "facts" in response.json()



def test_world_graph_survives_an_offline_graph_server(client):
    """A Neo4j outage is not an OSError: the driver raises GqlError subclasses."""
    _scenario, campaign = _bound_campaign(client, "Offline graph")
    response = client.get(f"/api/game/{campaign['id']}/world-graph")
    assert response.status_code == 200
    assert response.json() == {"nodes": [], "links": []}



# --- the wizard asks once, and traces of an unknown campaign are empty ------

def _scenario_with_question(client):
    from tests.conftest import make_scenario

    return make_scenario(
        client,
        title="初始化一次就够",
        setup_questions=[
            {"id": "q1", "var_name": "who", "prompt": "你是谁？", "type": "text", "required": True},
        ],
    )


def _campaign(client, scenario_id):
    campaign = client.post(
        f"/api/scenarios/{scenario_id}/campaigns", json={"player_name": "旅人"},
    ).json()
    return campaign.get("id") or campaign.get("campaign_id")


def test_setup_state_stops_asking_once_answers_are_saved(client):
    """Both wizard bugs were "the answer was not saved"; the backend half of the
    contract is that a non-empty answer set ends the initialization."""
    campaign_id = _campaign(client, _scenario_with_question(client)["id"])

    assert client.get(f"/api/game/{campaign_id}/setup-state").json()["needs_setup"] is True

    saved = client.post(
        f"/api/game/{campaign_id}/setup-answers",
        json={"answers": {"who": {"var_name": "who", "type": "text", "value": "抄书人"}}},
    )
    assert saved.status_code == 200

    after = client.get(f"/api/game/{campaign_id}/setup-state").json()
    assert after["needs_setup"] is False
    assert after["answers"]["who"]["value"] == "抄书人"


def test_an_empty_answer_set_keeps_the_wizard_open(client):
    """Storing {} is the same as storing nothing: a refresh must ask again."""
    campaign_id = _campaign(client, _scenario_with_question(client)["id"])
    client.post(f"/api/game/{campaign_id}/setup-answers", json={"answers": {}})
    assert client.get(f"/api/game/{campaign_id}/setup-state").json()["needs_setup"] is True


def test_traces_of_an_unknown_campaign_are_an_empty_list(client):
    r = client.get("/api/game/does-not-exist/traces")
    assert r.status_code == 200
    assert r.json() == {"traces": []}


def test_deleting_traces_of_an_unknown_campaign_deletes_nothing(client):
    r = client.delete("/api/game/does-not-exist/traces")
    assert r.status_code == 200
    assert r.json()["deleted"] == 0
