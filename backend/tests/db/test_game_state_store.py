"""GameStateStore: transactionality, idempotency and migration safety."""
import sqlite3

import pytest

from app.db.game_state_store import SCHEMA_VERSION, GameStateStore
from app.services.world_service import build_initial_state
from tests.support import bound_world, village_world


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def test_world_round_trips_through_storage(store):
    world = village_world()
    store.save_world(world)
    loaded = store.get_world("demo_village")
    assert loaded is not None
    assert loaded.name == world.name
    assert [location.id for location in loaded.locations] == [
        location.id for location in world.locations
    ]
    assert [event.id for event in loaded.events] == [event.id for event in world.events]
    assert store.list_worlds()[0]["world_id"] == "demo_village"


def test_state_round_trips_and_binding_is_idempotent(store):
    world = village_world()
    store.save_world(world)
    state = build_initial_state(world, "camp-1")
    store.bind_campaign(state)
    assert store.get_state("camp-1").model_dump() == state.model_dump()
    store.bind_campaign(state)
    assert store.get_state("camp-1").version == 0


def test_schema_migration_is_idempotent_across_instances(tmp_path):
    path = str(tmp_path / "game_state.db")
    first = GameStateStore(path)
    first.close()
    second = GameStateStore(path)
    try:
        row = second._conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION
        tables = {
            row["name"]
            for row in second._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"worlds", "campaign_state", "action_requests", "domain_events"} <= tables
    finally:
        second.close()


def test_duplicate_request_id_rolls_back_the_state_update(store):
    world, state = bound_world(store)
    events = [{"event_type": "action.applied", "payload": {"action": "take"}}]
    store.commit_action(
        campaign_id="camp-1",
        request_id="req-1",
        request_payload={"action": "take", "target_id": "iron_key"},
        status="applied",
        result_payload={"status": "applied"},
        state=state.model_copy(update={"version": 1, "inventory": {"iron_key": 1}}),
        events=events,
    )
    assert store.get_state("camp-1").version == 1

    with pytest.raises(sqlite3.IntegrityError):
        store.commit_action(
            campaign_id="camp-1",
            request_id="req-1",
            request_payload={"action": "look"},
            status="applied",
            result_payload={"status": "applied"},
            state=state.model_copy(update={"version": 2}),
            events=events,
        )
    stored = store.get_state("camp-1")
    assert stored.version == 1
    assert stored.inventory == {"iron_key": 1}
    assert len(store.list_events("camp-1")) == 1


def test_commit_without_bound_campaign_raises_and_leaves_no_request(store):
    world = village_world()
    store.save_world(world)
    with pytest.raises(ValueError):
        store.commit_action(
            campaign_id="camp-missing",
            request_id="req-x",
            request_payload={"action": "look"},
            status="applied",
            result_payload={"status": "applied"},
            state=build_initial_state(world, "camp-missing"),
            events=[],
        )
    assert store.get_request("req-x") is None


def test_get_request_exposes_the_stored_shape(store):
    bound_world(store)
    store.record_request(
        campaign_id="camp-1",
        request_id="req-1",
        request_payload={"action": "take", "target_id": "iron_key"},
        status="rejected",
        result_payload={"status": "rejected", "code": "item_not_here"},
        state_version=0,
    )
    row = store.get_request("req-1")
    assert row["campaign_id"] == "camp-1"
    assert row["status"] == "rejected"
    assert row["request"]["action"] == "take"
    assert row["result"]["code"] == "item_not_here"
    assert store.get_request("nope") is None


def test_list_events_respects_version_cursor(store):
    bound_world(store)
    store.record_request(
        campaign_id="camp-1",
        request_id="req-0",
        request_payload={"action": "look"},
        status="rejected",
        result_payload={"status": "rejected"},
        state_version=0,
    )
    state = store.get_state("camp-1")
    store.commit_action(
        campaign_id="camp-1",
        request_id="req-1",
        request_payload={"action": "take", "target_id": "iron_key"},
        status="applied",
        result_payload={"status": "applied"},
        state=state.model_copy(update={"version": 1}),
        events=[
            {"event_type": "action.applied", "state_version": 1},
            {"event_type": "event.fired", "state_version": 1},
        ],
    )
    assert len(store.list_events("camp-1")) == 2
    assert store.list_events("camp-1", after_version=1) == []
    assert len(store.list_events("camp-1", limit=1)) == 1


async def test_narrative_history_is_read_only_bounded_and_campaign_scoped(store):
    from app.models.game import ActionRequest
    from app.services.action_service import ActionService

    bound_world(store)
    service = ActionService(store)
    for index in range(8):
        await service.execute(ActionRequest(
            campaign_id="camp-1", request_id=f"r{index}", action="look",
            params={"text": f"  authored turn {index}  "},
        ))
    store.record_request(
        campaign_id="other", request_id="other", request_payload={"text": "private"},
        status="rejected", result_payload={"message": "secret"}, state_version=0,
    )
    store.record_request(
        campaign_id="camp-1", request_id="rejected", request_payload={"text": "unparsed"},
        status="rejected", result_payload={"message": "Not understood."}, state_version=8,
    )
    before = store._conn.total_changes
    store._conn.execute("PRAGMA query_only = ON")
    history = store.narrative_history("camp-1", limit=2)
    assert [row["request_id"] for row in history] == ["r7", "r7", "rejected", "rejected"]
    assert history[0]["payload"]["text"] == "  authored turn 7  "
    assert history[-1]["payload"]["text"] == "Not understood."
    assert len(store.narrative_history("camp-1")) == 18
    assert store.narrative_history("camp-1", limit=0) == []
    assert store.narrative_history("missing") == []
    assert store._conn.total_changes == before
