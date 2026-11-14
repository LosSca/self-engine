"""Time spent is time that passes for everyone.

`wait` used to move the clock and nothing else, so a night of sleep landed you
in a world that looked exactly like the one you left. These tests pin the new
contract: whatever comes due inside an action's own elapsed time fires in the
same transaction as that action, and a shift that ends asks for the next one.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.engines.game_clock import midnight
from app.models.game import ActionRequest
from app.services.action_service import ActionService
from app.services.world_service import bind_world, save_world
from tests.support import village_world


def _at(day: int, minute_of_day: int) -> int:
    return midnight(day, minute_of_day)


@pytest.fixture
def campaign(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world = village_world()
    save_world(store, world)
    state = bind_world(store, "camp-1", world.world_id, reset=True)
    yield store, state
    store.close()


def _wait(request_id: str, minutes: int) -> ActionRequest:
    return ActionRequest(
        campaign_id="camp-1",
        request_id=request_id,
        action="wait",
        params={"minutes": minutes},
        narrate=False,
    )


async def test_a_shift_that_starts_during_a_night_of_sleep_runs(campaign):
    store, state = campaign
    store.schedule_event(
        "camp-1", due_abs_minute=_at(1, 9 * 60), kind="forge_opens",
    )

    seen = []

    def open_forge(_store, current, entry):
        seen.append(entry["due_abs_minute"])
        return (
            current.model_copy(update={"flags": {**current.flags, "forge_open": True}}),
            [{"event_type": "world_tick", "payload": {"opened": "forge"}}],
            [],
        )

    service = ActionService(store, tick_handlers={"forge_opens": open_forge})
    result = await service.execute(_wait("w1", 120))

    assert result.status == "applied"
    assert seen == [_at(1, 9 * 60)]
    saved = store.get_state("camp-1")
    assert saved.flags.get("forge_open") is True
    assert saved.minute == 10 * 60


async def test_the_tick_is_part_of_the_same_commit(campaign):
    store, state = campaign
    store.schedule_event("camp-1", due_abs_minute=_at(1, 9 * 60), kind="forge_opens")
    service = ActionService(store)

    result = await service.execute(_wait("w1", 120))
    events = store.list_events("camp-1")
    kinds = [event["event_type"] for event in events]

    # The turn's own fact and the world's are in the same transaction, so a
    # reader can never see the clock advanced without the events it passed.
    assert "action.applied" in kinds
    assert "WORLD_TICK" in kinds
    assert result.state.version == store.get_state("camp-1").version


async def test_nothing_scheduled_means_no_tick_rows(campaign):
    store, state = campaign
    service = ActionService(store)
    result = await service.execute(_wait("w1", 120))

    kinds = [event["event_type"] for event in store.list_events("camp-1")]
    assert "world_tick" not in kinds
    assert result.state.minute == 10 * 60


async def test_a_handler_can_ask_for_the_next_shift(campaign):
    store, state = campaign
    store.schedule_event("camp-1", due_abs_minute=_at(1, 9 * 60), kind="shift_start")

    def start_shift(_store, current, entry):
        return (
            current,
            [],
            [
                {
                    "kind": "shift_end",
                    "due_abs_minute": _at(1, 18 * 60),
                    "payload": {"npc": "smith"},
                }
            ],
        )

    service = ActionService(store, tick_handlers={"shift_start": start_shift})
    await service.execute(_wait("w1", 120))

    pending = store.list_scheduled_events("camp-1", status="pending")
    assert [entry["kind"] for entry in pending] == ["shift_end"]
    assert store.next_due_minute("camp-1") == _at(1, 18 * 60)


async def test_a_short_action_still_fires_an_event_that_lands_midway(campaign):
    """`look` costs 0 minutes, so nothing can come due inside it."""
    store, state = campaign
    store.schedule_event("camp-1", due_abs_minute=_at(1, 8 * 60), kind="now")
    service = ActionService(store)
    result = await service.execute(
        ActionRequest(
            campaign_id="camp-1", request_id="look-1", action="look", narrate=False
        )
    )
    assert result.status == "applied"
    # A zero-minute action cannot advance past the event; it waits for real time
    # to be spent, which is the only honest answer.
    assert store.list_scheduled_events("camp-1", status="pending")[0]["kind"] == "now"


async def test_replaying_a_wait_does_not_fire_the_same_event_twice(campaign):
    store, state = campaign
    store.schedule_event("camp-1", due_abs_minute=_at(1, 9 * 60), kind="forge_opens")
    calls = []

    def open_forge(_store, current, entry):
        calls.append(1)
        return current, [], []

    service = ActionService(store, tick_handlers={"forge_opens": open_forge})
    first = await service.execute(_wait("w1", 120))
    replay = await service.execute(_wait("w1", 120))

    assert replay.status == "duplicate"
    assert replay.state_version == first.state.version
    assert calls == [1]


async def test_a_paused_clock_neither_moves_nor_fires(campaign):
    store, state = campaign
    store.schedule_event("camp-1", due_abs_minute=_at(1, 9 * 60), kind="forge_opens")
    calls = []

    def open_forge(_store, current, entry):
        calls.append(1)
        return current, [], []

    service = ActionService(
        store, tick_handlers={"forge_opens": open_forge}, clock_paused=lambda _c: True
    )
    await service.execute(_wait("w1", 120))

    assert calls == []
    assert store.get_state("camp-1").minute == state.minute
