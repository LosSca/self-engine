"""End-to-end action processing against real SQLite storage."""
import sqlite3

import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import (
    ActionRequest, Condition, Effect, GameEventDef, NarrativeResult,
    ObjectiveDef, QuestDef, RuleOutcome,
)
from app.services.action_service import ActionService
from app.services.world_service import bind_world, save_world
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
        # The prose that was actually shown is part of the record, so a
        # narration mirror (knowledge graph) can replay it verbatim.
        "narrator.response",
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


async def test_a_failing_settled_sink_cannot_fail_the_turn(store):
    """The knowledge graph mirror runs after the commit and may fail freely."""
    bound_world(store)
    seen = []

    def sink(campaign_id, state, narrative):
        seen.append((campaign_id, state.version, narrative.status))
        raise RuntimeError("neo4j is down")

    service = ActionService(store, settled_sink=sink)
    result = await service.execute(_request("req-look", "look"))

    assert result.status == "applied"
    assert store.get_state("camp-1").version == 1
    assert seen == [("camp-1", 1, "ok")]


async def test_a_duplicate_request_does_not_mirror_twice(store):
    bound_world(store)
    seen = []
    service = ActionService(store, settled_sink=lambda *args: seen.append(args[1].version))

    await service.execute(_request("req-take", "take", "iron_key"))
    replay = await service.execute(_request("req-take", "take", "iron_key"))

    assert replay.status == "duplicate"
    assert seen == [1], "a replay describes the past, it does not happen again"


def _bind_quests(store, quests, events=()):
    world = village_world()
    world.quests = quests
    world.events = list(events)
    save_world(store, world)
    bind_world(store, "camp-1", world.world_id, reset=True)
    return world


def _item_quest(quest_id, rewards=(), required_count=1):
    return QuestDef(
        id=quest_id, title=quest_id,
        objectives=[ObjectiveDef(
            id="keys", kind="obtain_item", target="iron_key", required_count=required_count
        )],
        rewards=list(rewards),
    )


async def test_failed_rewards_retry_on_next_action_not_same_turn_or_duplicate(store):
    _bind_quests(store, [
        _item_quest("retry", [
            Effect(kind="give_item", target="blessing_token", amount=2),
            Effect(kind="set_flag", target="reward_granted", value=True),
            Effect(kind="remove_item", target="iron_key", amount=2),
        ]),
        _item_quest("supply", [Effect(kind="set_flag", target="supply_ready", value=True)]),
    ], [GameEventDef(
        id="supply_key", name="Supply key", trigger="state_change",
        conditions=[Condition(kind="flag", target="supply_ready", value=True)],
        effects=[Effect(kind="give_item", target="iron_key")],
    )])
    service = ActionService(store)
    request = _request("req-take", "take", "iron_key", narrate=False)
    first = await service.execute(request)
    assert first.status == "applied"
    assert first.state.quests["retry"].status == "active"
    assert first.state.quests["retry"].objectives == {"keys": 1}
    assert first.state.inventory == {"iron_key": 2}
    assert "reward_granted" not in first.state.flags
    events = store.list_events("camp-1")
    retry_events = [event for event in events if event["payload"].get("quest_id") == "retry"]
    assert [event["event_type"] for event in retry_events] == ["quest.reward_failed"]
    assert "only 1 held" in retry_events[0]["payload"]["error"]
    replay = await service.execute(request)
    assert replay.status == "duplicate"
    assert replay.state == first.state
    assert store.list_events("camp-1") == events

    # Look emits no new objective facts; the satisfied quest still retries.
    retried = await service.execute(_request("req-look", "look", narrate=False))
    assert retried.status == "applied"
    assert retried.state.quests["retry"].status == "completed"
    assert retried.state.inventory == {"blessing_token": 2}
    assert retried.state.flags["reward_granted"] is True
    completed = [
        event for event in store.list_events("camp-1")
        if event["event_type"] == "quest.completed" and event["payload"]["quest_id"] == "retry"
    ]
    assert len(completed) == 1
    assert len(completed[0]["payload"]["rewards"]) == 3
    again = await service.execute(_request("req-look-again", "look", narrate=False))
    assert again.state.inventory == {"blessing_token": 2}


async def test_reward_event_quest_chains_settle_without_recounting_facts_or_always_events(store):
    quests = [_item_quest("chain_0", [
        Effect(kind="set_flag", target="gate_0", value=True),
        Effect(kind="give_item", target="blessing_token", amount=2),
    ])]
    events = [GameEventDef(
        id="always", name="Always", trigger="state_change", repeat="always",
        effects=[Effect(kind="give_item", target="blessing_token")],
    )]
    for index in range(1, 5):
        quests.append(QuestDef(
            id=f"chain_{index}", title=f"Chain {index}",
            objectives=[ObjectiveDef(id="flag", kind="flag", target=f"step_{index}")],
            rewards=[Effect(kind="set_flag", target=f"gate_{index}", value=True)],
        ))
        events.append(GameEventDef(
            id=f"event_{index}", name=f"Event {index}", trigger="state_change",
            priority=index,
            conditions=[Condition(kind="flag", target=f"gate_{index - 1}", value=True)],
            effects=[
                Effect(kind="set_flag", target=f"step_{index}", value=True),
                Effect(kind="give_item", target="blessing_token"),
            ],
        ))
    quests.extend([
        _item_quest("key_counter", required_count=2),
        QuestDef(id="reward_counter", title="Reward counter", objectives=[
            ObjectiveDef(id="tokens", kind="obtain_item", target="blessing_token", required_count=100)
        ]),
    ])
    _bind_quests(store, quests, events)
    service = ActionService(store)
    result = await service.execute(_request("req-take", "take", "iron_key", narrate=False))
    assert result.status == "applied"
    assert all(result.quests[f"chain_{index}"] == "completed" for index in range(5))
    assert result.state.flags["gate_4"] is True
    assert result.state.inventory["blessing_token"] == 7
    assert result.state.quests["reward_counter"].objectives["tokens"] == 7
    assert result.state.quests["key_counter"].objectives["keys"] == 1
    fired = [execution.event_id for execution in result.events_fired if execution.status == "fired"]
    assert sorted(fired) == ["always", "event_1", "event_2", "event_3", "event_4"]
    completed = [event for event in store.list_events("camp-1") if event["event_type"] == "quest.completed"]
    assert len(completed) == 5
    following = await service.execute(_request("req-look", "look", narrate=False))
    assert following.state.inventory["blessing_token"] == 8
    assert following.state.quests["reward_counter"].objectives["tokens"] == 8


async def test_explicit_completion_grants_once_and_records_quest_and_reward_facts(store):
    _bind_quests(store, [
        _item_quest("explicit", [Effect(kind="give_item", target="blessing_token", amount=3)]),
        QuestDef(id="collector", title="Collector", objectives=[
            ObjectiveDef(id="tokens", kind="obtain_item", target="blessing_token", required_count=3)
        ]),
    ], [GameEventDef(
        id="finish", name="Finish", trigger="action", trigger_action="look", repeat="always",
        effects=[Effect(kind="quest_complete", target="explicit")],
    )])
    service = ActionService(store)
    result = await service.execute(_request("req-look", "look", narrate=False))
    assert result.quests == {"explicit": "completed", "collector": "completed"}
    assert result.state.inventory["blessing_token"] == 3
    completed = [event for event in store.list_events("camp-1") if event["event_type"] == "quest.completed"]
    assert {event["payload"]["quest_id"] for event in completed} == {"explicit", "collector"}
    again = await service.execute(_request("req-look-again", "look", narrate=False))
    assert again.state.inventory["blessing_token"] == 3
    assert not [
        event for event in store.list_events("camp-1", after_version=1)
        if event["event_type"] == "quest.completed"
    ]


async def test_explicit_completion_reward_failure_rolls_back_the_enclosing_event(store):
    _bind_quests(store, [_item_quest("explicit", [
        Effect(kind="give_item", target="blessing_token"),
        Effect(kind="remove_item", target="iron_key"),
    ])], [GameEventDef(
        id="finish", name="Finish", trigger="action", trigger_action="look",
        effects=[
            Effect(kind="set_flag", target="event_prefix", value=True),
            Effect(kind="quest_complete", target="explicit"),
        ],
    )])
    result = await ActionService(store).execute(_request("req-look", "look", narrate=False))
    assert result.status == "applied"
    assert result.quests["explicit"] == "active"
    assert result.state.inventory == {}
    assert "event_prefix" not in result.state.flags
    assert result.events_fired[0].status == "failed"
    assert "reward failed" in result.events_fired[0].reason
    assert [event["event_type"] for event in store.list_events("camp-1")] == [
        "action.applied", "event.failed"
    ]


@pytest.mark.parametrize("explicit", [False, True])
async def test_failed_quests_emit_failed_not_progressed_or_completed(store, explicit):
    quest = _item_quest("doomed")
    if not explicit:
        quest.fail_conditions = [Condition(kind="flag", target="doomed", value=True)]
    _bind_quests(store, [quest], [GameEventDef(
        id="fail", name="Fail", trigger="action", trigger_action="look",
        effects=[Effect(kind="quest_fail", target="doomed") if explicit else
                 Effect(kind="set_flag", target="doomed", value=True)],
    ), GameEventDef(
        id="after_failure", name="After failure", trigger="state_change", priority=10,
        conditions=[Condition(kind="quest_status", target="doomed", value="failed")],
        effects=[Effect(kind="set_flag", target="failure_seen", value=True)],
    )])
    result = await ActionService(store).execute(_request("req-look", "look", narrate=False))
    assert result.quests["doomed"] == "failed"
    assert result.state.flags["failure_seen"] is True
    assert [
        event["event_type"] for event in store.list_events("camp-1")
        if event["event_type"].startswith("quest.")
    ] == ["quest.failed"]


@pytest.mark.parametrize("different_payload", [False, True])
async def test_concurrent_rejected_insert_replays_or_conflicts_without_state_changes(
    store, monkeypatch, different_payload
):
    bound_world(store)
    before = store.get_state("camp-1")
    record = store.record_request

    def race(**kwargs):
        winning = dict(kwargs)
        if different_payload:
            winning["request_payload"] = {**kwargs["request_payload"], "action": "look"}
        record(**winning)
        record(**kwargs)

    monkeypatch.setattr(store, "record_request", race)
    result = await ActionService(store).execute(
        _request("req-open", "open", "warehouse_door", narrate=False)
    )
    assert result.status == ("conflict" if different_payload else "duplicate")
    assert store.get_state("camp-1") == before
    assert store.list_events("camp-1") == []


async def test_concurrent_effect_rejection_is_also_replayed(store, monkeypatch):
    bound_world(store)
    before = store.get_state("camp-1")
    record = store.record_request

    class InvalidRules:
        def evaluate(self, *args):
            return RuleOutcome(ok=True, code="test", effects=[
                Effect(kind="give_item", target="blessing_token"),
                Effect(kind="remove_item", target="iron_key"),
            ])

    def race(**kwargs):
        record(**kwargs)
        record(**kwargs)

    monkeypatch.setattr(store, "record_request", race)
    result = await ActionService(store, rules=InvalidRules()).execute(
        _request("req-look", "look", narrate=False)
    )
    assert result.status == "duplicate"
    assert result.code == "effect_failed"
    assert store.get_state("camp-1") == before
    assert store.list_events("camp-1") == []
