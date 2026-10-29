"""End-to-end action processing against real SQLite storage."""
import sqlite3

import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import ActionRequest, NarrativeResult
from app.services.action_service import ActionService
from tests.support import bound_world, village_world


class FailingNarrator:
    """Narration blows up; committed game state must survive it."""

    def __init__(self):
        self.calls = 0

    async def narrate(self, request, outcome, state, world, executions=(), quest_updates=()):
        self.calls += 1
        return NarrativeResult(status="failed", error="narrator unavailable")


class ExplodingNarrator:
    """The model client raises instead of returning a failed result."""

    def __init__(self):
        self.calls = 0

    async def narrate(self, *args, **kwargs):
        self.calls += 1
        raise TimeoutError("model call timed out")


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def _request(request_id, action, target=None, **kwargs):
    return ActionRequest(
        campaign_id="camp-1",
        request_id=request_id,
        action=action,
        target_id=target,
        **kwargs,
    )


# ------------------------------------------------------------------ the slice
async def test_pick_up_key_then_open_locked_door(store):
    world, state = bound_world(store)
    service = ActionService(store)

    taken = await service.execute(_request("req-take", "take", "iron_key"))
    assert taken.status == "applied"
    assert taken.ok and taken.code == "taken"
    assert taken.state_version == 1
    assert taken.state.inventory == {"iron_key": 1}
    assert taken.state.flags == {"key_taken": True}
    assert taken.state.quests["open_the_warehouse"].objectives["find_key"] == 1

    opened = await service.execute(_request("req-open", "open", "warehouse_door"))
    assert opened.status == "applied"
    assert opened.ok and opened.code == "door_unlocked"
    assert opened.state_version == 2
    assert opened.state.doors["warehouse_door"] is True
    assert opened.state.flags["warehouse_opened"] is True
    assert opened.state.quests["open_the_warehouse"].status == "completed"
    assert opened.state.inventory["blessing_token"] == 1
    assert opened.quests["open_the_warehouse"] == "completed"
    assert opened.narrative.status == "ok"
    assert "Iron Key" in opened.narrative.text

    stored = store.get_state("camp-1")
    assert stored.version == 2
    assert stored.doors["warehouse_door"] is True
    assert stored.inventory == {"iron_key": 1, "blessing_token": 1}


async def test_open_door_without_key_is_rejected_and_state_unchanged(store):
    bound_world(store)
    service = ActionService(store)

    rejected = await service.execute(_request("req-open", "open", "warehouse_door"))
    assert rejected.status == "rejected"
    assert not rejected.ok
    assert rejected.code == "door_locked"
    assert rejected.state_version == 0
    assert rejected.state.doors["warehouse_door"] is False
    assert rejected.narrative.status == "ok"
    assert store.get_state("camp-1").version == 0
    assert store.list_events("camp-1") == []


async def test_duplicate_request_applies_side_effects_once(store):
    bound_world(store)
    service = ActionService(store)

    first = await service.execute(_request("req-take", "take", "iron_key"))
    second = await service.execute(_request("req-take", "take", "iron_key"))

    assert first.status == "applied"
    assert second.status == "duplicate"
    assert second.state_version == 1
    stored = store.get_state("camp-1")
    assert stored.version == 1
    assert stored.inventory == {"iron_key": 1}
    assert second.events_fired == first.events_fired
    assert [event["event_type"] for event in store.list_events("camp-1")] == [
        "action.applied",
        "event.fired",
        "quest.progressed",
    ]


async def test_same_request_id_with_different_payload_is_a_conflict(store):
    bound_world(store)
    service = ActionService(store)

    await service.execute(_request("req-1", "take", "iron_key"))
    conflict = await service.execute(_request("req-1", "look"))
    assert conflict.status == "conflict"
    assert conflict.code == "request_id_conflict"
    assert store.get_state("camp-1").inventory == {"iron_key": 1}


async def test_rejected_request_is_replayable_without_side_effects(store):
    bound_world(store)
    service = ActionService(store)

    await service.execute(_request("req-open", "open", "warehouse_door"))
    again = await service.execute(_request("req-open", "open", "warehouse_door"))
    assert again.status == "duplicate"
    assert store.get_state("camp-1").doors["warehouse_door"] is False


async def test_narrative_failure_does_not_roll_back_committed_state(store):
    bound_world(store)
    narrator = FailingNarrator()
    service = ActionService(store, narrator=narrator)

    result = await service.execute(_request("req-take", "take", "iron_key"))
    assert result.status == "applied"
    assert result.narrative.status == "failed"
    assert "narrator unavailable" in result.narrative.error
    assert narrator.calls == 1

    stored = store.get_state("camp-1")
    assert stored.version == 1
    assert stored.inventory == {"iron_key": 1}
    event_types = [event["event_type"] for event in store.list_events("camp-1")]
    assert "narrative.failed" in event_types


async def test_state_and_events_share_the_committed_version(store):
    bound_world(store)
    service = ActionService(store)
    await service.execute(_request("req-take", "take", "iron_key"))
    await service.execute(_request("req-open", "open", "warehouse_door"))

    events = store.list_events("camp-1")
    assert [event["state_version"] for event in events] == sorted(
        event["state_version"] for event in events
    )
    assert {event["state_version"] for event in events} == {1, 2}
    types = [event["event_type"] for event in events]
    assert types[0] == "action.applied"
    assert "quest.completed" in types


async def test_reloaded_store_keeps_state_and_still_detects_duplicates(store):
    bound_world(store)
    service = ActionService(store)
    await service.execute(_request("req-take", "take", "iron_key"))

    reopened = GameStateStore(store.db_path)
    try:
        state = reopened.get_state("camp-1")
        assert state.version == 1
        assert state.inventory == {"iron_key": 1}

        replay = await ActionService(reopened).execute(_request("req-take", "take", "iron_key"))
        assert replay.status == "duplicate"
        assert reopened.get_state("camp-1").version == 1
    finally:
        reopened.close()


async def test_failed_commit_does_not_leak_state(store):
    bound_world(store)
    store.record_request(
        campaign_id="camp-1",
        request_id="req-take",
        request_payload={"action": "look"},
        status="applied",
        result_payload={"status": "applied"},
        state_version=0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_action(
            campaign_id="camp-1",
            request_id="req-take",
            request_payload={"action": "take"},
            status="applied",
            result_payload={"status": "applied"},
            state=store.get_state("camp-1").model_copy(update={"version": 99}),
            events=[],
        )
    assert store.get_state("camp-1").version == 0


async def test_narrator_exception_still_commits_the_turn(store):
    """A raising model client must not undo a rules-decided action."""
    bound_world(store)
    narrator = ExplodingNarrator()
    service = ActionService(store, narrator=narrator)

    result = await service.execute(_request("req-take", "take", "iron_key"))

    assert result.status == "applied"
    assert result.state_version == 1
    assert result.narrative.status == "failed"
    assert "TimeoutError" in result.narrative.error
    stored = store.get_state("camp-1")
    assert stored.version == 1
    assert stored.inventory == {"iron_key": 1}
    assert "narrative.failed" in [
        event["event_type"] for event in store.list_events("camp-1")
    ]


async def test_narrator_exception_on_a_rejected_action_is_reported_not_raised(store):
    bound_world(store)
    service = ActionService(store, narrator=ExplodingNarrator())

    result = await service.execute(_request("req-open", "open", "warehouse_door"))

    assert result.status == "rejected"
    assert result.code == "door_locked"
    assert result.narrative.status == "failed"
    assert store.get_state("camp-1").version == 0
