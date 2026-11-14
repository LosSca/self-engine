"""The world clock moves without the player, and remembers what it owes.

These tests pin the two properties Phase 2 has to earn:

* time is not a side effect of typing - a tick advances the authoritative clock
  and records what fired, all in one transaction;
* a scheduled event survives a restart, so "the forge opens at 08:00" is still
  true after the process died.

Nothing here calls an LLM.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.engines.game_clock import MINUTES_PER_DAY, midnight
from app.engines.world_scheduler import (
    catch_up_minute,
    commit_tick,
    plan_tick,
    run_tick,
)
from app.services.world_service import bind_world, save_world
from tests.support import village_world


@pytest.fixture
def store_and_state(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world = village_world()
    save_world(store, world)
    state = bind_world(store, "camp-1", world.world_id, reset=True)
    yield store, state
    store.close()


def _at(day: int, minute_of_day: int) -> int:
    return midnight(day, minute_of_day)


async def test_an_empty_tick_still_advances_the_clock(store_and_state):
    store, state = store_and_state
    start = _at(1, 8 * 60)
    target = _at(1, 12 * 60)

    plan = plan_tick(store, "camp-1", target)
    assert plan.empty

    new_state, events, scheduled, fired = await run_tick(
        store, state, plan, {},
        from_abs_minute=start, to_abs_minute=target,
    )
    assert events == [] and scheduled == [] and fired == []
    assert new_state.minute == 12 * 60

    outcome = commit_tick(
        store, "camp-1", new_state,
        from_abs_minute=start, to_abs_minute=target,
        events=events, fired_ids=fired,
    )
    assert outcome.minutes_advanced == 4 * 60
    saved = store.get_state("camp-1")
    assert saved.minute == 12 * 60
    assert saved.version == state.version + 1


async def test_a_scheduled_event_fires_at_its_own_minute(store_and_state):
    store, state = store_and_state
    store.schedule_event(
        "camp-1", due_abs_minute=_at(1, 9 * 60), kind="forge_opens",
        payload={"kind": "forge_opens", "npc": "smith"},
    )

    calls = []

    def forge_opens(_store, current, entry):
        calls.append(entry["due_abs_minute"])
        return current.model_copy(update={"flags": {"forge_open": True}}), [
            {"event_type": "world_tick", "payload": {"forge": "open"}}
        ], []

    target = _at(1, 15 * 60)
    plan = plan_tick(store, "camp-1", target)
    new_state, events, _scheduled, fired = await run_tick(
        store, state, plan, {"forge_opens": forge_opens}
    )

    assert calls == [_at(1, 9 * 60)]
    assert new_state.flags["forge_open"] is True
    # The tick only knows one stop: the due event. The caller ticks again to
    # reach the target, which is what keeps causality intact.
    assert len(fired) == 1


async def test_catch_up_stops_at_the_next_due_event(store_and_state):
    store, _state = store_and_state
    # With nothing scheduled the hop goes past the requested minute: the answer
    # is always "something later than now", so a tick loop cannot stall.
    assert catch_up_minute(store, "camp-1", _at(1, 20 * 60)) == _at(1, 20 * 60) + 1

    # A due event in the *past* has already happened; it must not hold the clock
    # back to a minute the world has left behind.
    store.schedule_event("camp-1", due_abs_minute=_at(1, 10 * 60), kind="shift")
    assert catch_up_minute(store, "camp-1", _at(1, 20 * 60)) == _at(1, 20 * 60) + 1

    # The next event ahead of us is where the hop stops, so it is applied at its
    # own minute.
    store.schedule_event("camp-1", due_abs_minute=_at(1, 22 * 60), kind="tonight")
    past = sorted(store.list_scheduled_events("camp-1", status="pending"), key=lambda row: row["due_abs_minute"])[0]
    assert past["kind"] == "shift"
    store.mark_event_fired(past["id"])
    assert catch_up_minute(store, "camp-1", _at(1, 19 * 60)) == _at(1, 22 * 60)

    # Standing before both events again, the next one ahead is still the stop.
    assert catch_up_minute(store, "camp-1", _at(1, 5 * 60)) == _at(1, 22 * 60)


async def test_events_run_in_causal_order_not_insertion_order(store_and_state):
    store, state = store_and_state
    order = []
    store.schedule_event("camp-1", due_abs_minute=_at(1, 11 * 60), kind="late")
    store.schedule_event("camp-1", due_abs_minute=_at(1, 9 * 60), kind="early")

    def handler(_store, current, entry):
        order.append(entry["kind"])
        return current, [], []

    plan = plan_tick(store, "camp-1", _at(1, 12 * 60))
    await run_tick(store, state, plan, {"early": handler, "late": handler})
    assert order == ["early", "late"]


async def test_same_minute_events_break_ties_by_priority_then_sequence(store_and_state):
    store, state = store_and_state
    order = []
    # Higher priority number = runs later; then first scheduled wins.
    e1 = store.schedule_event(
        "camp-1", due_abs_minute=_at(1, 9 * 60), kind="a", priority=50,
    )
    e2 = store.schedule_event(
        "camp-1", due_abs_minute=_at(1, 9 * 60), kind="b", priority=10,
    )
    e3 = store.schedule_event(
        "camp-1", due_abs_minute=_at(1, 9 * 60), kind="c", priority=50,
    )
    assert e1["sequence_id"] < e3["sequence_id"]

    plan = plan_tick(store, "camp-1", _at(1, 10 * 60))
    assert [entry["kind"] for entry in plan.entries] == ["b", "a", "c"]
    del order, e2


async def test_a_failing_handler_is_recorded_and_does_not_stop_the_world(store_and_state):
    store, state = store_and_state
    store.schedule_event("camp-1", due_abs_minute=_at(1, 9 * 60), kind="broken")
    store.schedule_event("camp-1", due_abs_minute=_at(1, 9 * 60), kind="fine")

    ran = []

    def broken(_store, current, entry):
        raise RuntimeError("the forge caught fire")

    def fine(_store, current, entry):
        ran.append(True)
        return current, [], []

    plan = plan_tick(store, "camp-1", _at(1, 10 * 60))
    _new, events, _scheduled, fired = await run_tick(
        store, state, plan, {"broken": broken, "fine": fine}
    )
    assert ran == [True]
    assert len(fired) == 2
    failure = [e for e in events if e["payload"].get("status") == "failed"]
    assert len(failure) == 1
    assert "the forge caught fire" in failure[0]["payload"]["error"]


async def test_a_schedule_survives_a_restart(tmp_path):
    path = str(tmp_path / "game_state.db")
    store = GameStateStore(path)
    world = village_world()
    save_world(store, world)
    bind_world(store, "camp-1", world.world_id, reset=True)
    store.schedule_event(
        "camp-1", due_abs_minute=_at(2, 6 * 60), kind="baker_opens",
    )
    store.close()

    reopened = GameStateStore(path)
    try:
        assert reopened.next_due_minute("camp-1") == _at(2, 6 * 60)
        pending = reopened.list_scheduled_events("camp-1", status="pending")
        assert [entry["kind"] for entry in pending] == ["baker_opens"]
    finally:
        reopened.close()


async def test_committed_events_are_marked_fired_once(store_and_state):
    store, state = store_and_state
    entry = store.schedule_event(
        "camp-1", due_abs_minute=_at(1, 9 * 60), kind="shift",
    )
    plan = plan_tick(store, "camp-1", _at(1, 9 * 60))
    new_state, events, _scheduled, fired = await run_tick(store, state, plan, {})

    commit_tick(
        store, "camp-1", new_state,
        from_abs_minute=_at(1, 8 * 60), to_abs_minute=_at(1, 9 * 60),
        events=events, fired_ids=fired,
    )

    assert entry["id"] in fired
    assert store.due_events("camp-1", _at(9, 0)) == []
    # Asking to fire it again is a no-op, never a double booking.
    assert store.mark_event_fired(entry["id"]) is False


async def test_a_tick_writes_a_request_row_so_it_can_be_replayed(store_and_state):
    store, state = store_and_state
    plan = plan_tick(store, "camp-1", _at(1, 18 * 60))
    new_state, events, _s, fired = await run_tick(store, state, plan, {})
    outcome = commit_tick(
        store, "camp-1", new_state,
        from_abs_minute=_at(1, 8 * 60), to_abs_minute=_at(1, 18 * 60),
        events=events, fired_ids=fired,
    )

    row = store.get_request(outcome.request_id)
    assert row is not None
    assert row["request"]["action"] == "world_tick"
    assert row["request"]["actor_type"] == "system"
    assert row["status"] == "applied"


async def test_paused_clock_does_not_move(store_and_state):
    store, state = store_and_state
    plan = plan_tick(store, "camp-1", _at(3, 20 * 60))
    new_state, _events, _s, _f = await run_tick(
        store, state, plan, {}, clock_paused=True
    )
    assert new_state.day == state.day
    assert new_state.minute == state.minute


async def test_the_plan_is_capped_and_says_so(store_and_state):
    store, state = store_and_state
    for index in range(5):
        store.schedule_event(
            "camp-1", due_abs_minute=_at(1, 9 * 60), kind=f"e{index}",
        )
    plan = plan_tick(store, "camp-1", _at(1, 10 * 60), max_events=2)
    assert len(plan.entries) == 2
    assert plan.capped is True


async def test_has_pending_event_dedupes_recurring_schedules(store_and_state):
    store, _state = store_and_state
    assert store.has_pending_event("camp-1", "work_shift") is False
    entry = store.schedule_event("camp-1", due_abs_minute=_at(1, 9 * 60), kind="work_shift")
    assert store.has_pending_event("camp-1", "work_shift") is True
    store.mark_event_fired(entry["id"])
    assert store.has_pending_event("camp-1", "work_shift") is False


async def test_a_day_rollover_lands_on_the_right_day(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    try:
        world = village_world()
        save_world(store, world)
        state = bind_world(store, "camp-1", world.world_id, reset=True)
        # Start on day 1 at 23:30 and advance past midnight.
        state = state.model_copy(update={"day": 1, "minute": 23 * 60 + 30})
        store.bind_campaign(state)
        target = _at(2, 0) + 30
        plan = plan_tick(store, "camp-1", target)
        new_state, _e, _s, _f = await run_tick(
            store, state, plan, {},
            from_abs_minute=_at(1, 23 * 60 + 30), to_abs_minute=target,
        )
        assert new_state.day == 2
        assert new_state.minute == 30
        # Absolute minutes are 1-based in days: day 2 starts at 1440, not 2880.
        assert _at(2, 0) // MINUTES_PER_DAY == 1
    finally:
        store.close()
