"""A town that keeps running: NPC rounds ride on the player's own turn."""
import pytest
from fastapi.testclient import TestClient

from app.engines import npc_brain, npc_goals, npc_schedule

API = "/api/v1"


def _town() -> dict:
    """A village with one scheduled villager, built from the shared fixture."""
    from tests.support import village_world_data

    data = village_world_data()
    data["locations"][0]["npc_ids"] = ["elder_mara"]
    data["entities"] = [
        {
            "id": "elder_mara",
            "name": "Elder Mara",
            "kind": "npc",
            "description": "The elder keeps the square.",
            "location_id": "village_square",
            # Greedy and sociable, barely hungry: the villager wants to *do*
            # something every minute, which is what makes the round visible.
            "traits": {
                "diligence": 0.9,
                "sociability": 0.9,
                "greed": 0.9,
                "caution": 0.2,
                "boldness": 0.6,
            },
            "schedule": [
                {"start": "08:00", "activity": "work", "location_id": "village_square"},
            ],
        }
    ]
    return data


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("ENGINE_GRAPH_MIRROR", "0")
    monkeypatch.setenv("ENGINE_NARRATOR", "template")
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    from app.main import app

    return TestClient(app)


def _bound(client) -> None:
    created = client.post(f"{API}/worlds", json=_town())
    assert created.status_code == 200, created.text
    bound = client.post(f"{API}/campaigns/camp-1/world", json={"world_id": "demo_village"})
    assert bound.status_code == 200, bound.text


def _act(client, request_id, action, target=None, params=None):
    body = {
        "campaign_id": "camp-1",
        "request_id": request_id,
        "action": action,
        "target_id": target,
    }
    if params:
        body["params"] = params
    response = client.post(f"{API}/campaigns/camp-1/actions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _npc_turns(client):
    """NPC turns are ordinary requests whose ids carry the npc prefix."""
    rows = client.get(f"{API}/campaigns/camp-1/events", params={"limit": 200}).json()
    return [
        row
        for row in rows
        if str(row.get("request_id") or "").startswith("npc-")
    ]


def test_the_player_turn_is_committed_before_the_town_reacts(client):
    _bound(client)
    body = _act(client, "req-look", "look")
    assert body["status"] == "applied"
    # The player's own turn owns version 1; whatever the town did came after it,
    # never before, and never in place of it.
    assert body["state_version"] == 1
    state = client.get(f"{API}/campaigns/camp-1/state").json()
    assert state["version"] >= 1


def test_an_npc_turn_is_an_ordinary_request_through_the_same_rules(client):
    _bound(client)
    _act(client, "req-look", "look")
    turns = _npc_turns(client)
    assert turns, "the villager had nothing to do after the player looked around"
    for row in turns:
        stored = client.get(f"{API}/actions/{row['request_id']}")
        assert stored.status_code == 200, stored.text
        request = stored.json()["request"]
        # Same endpoint, same validation, same rules engine - the only thing
        # that says "an NPC did this" is the actor fields on the request.
        assert request["actor_type"] == "npc"
        assert request["campaign_id"] == "camp-1"
        assert request["action"]
        assert row["state_version"] >= 2


def test_player_and_npc_share_one_action_pipeline(client):
    from app.api.routes_engine import _service

    service = _service()
    try:
        assert service.npc_round is not None
        assert npc_schedule.NPC_ROUTINE in service.tick_handlers
    finally:
        service.store.close()
        service.scenario_store.close()


def test_the_same_inputs_always_choose_the_same_intent():
    from tests.services.test_npc_turns import _world

    world = _world()
    npc = world.entities[0]
    needs = {"hunger": 0.2, "energy": 0.9, "social": 0.9}

    def order():
        scored = npc_goals.score_intents(
            npc, needs, has_work=True, can_work_here=False, at_work=False
        )
        return [intent.name for intent in scored]

    first = order()
    assert first == order()
    # The order is a total one: nothing ties, so no run can disagree.
    assert len(set(first)) == len(first)
    assert first[0] == "eat"  # hunger is high, and hunger dominates


def test_an_npc_only_knows_what_it_witnessed(client):
    """Knowledge boundary: witnessing is the ONLY door into an NPC's beliefs."""
    from app.api.routes_engine import _get_store
    from tests.services.test_npc_turns import _world

    store = _get_store()
    try:
        from app.services import world_service

        world = _world()
        world_service.save_world(store, world)
        npc = world.entities[0]
        seen = npc_brain.perceive(store, "c-empty", npc)
        assert seen == {"something_is_wrong": False, "ore_missing": False}
    finally:
        store.close()
