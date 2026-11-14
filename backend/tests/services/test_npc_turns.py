"""Phase 3, integration half: an NPC turns a decision into a committed turn.

These use the real store, the real ActionService and the real rules engine, so
what is asserted here is the claim the design makes: NPC behaviour is not a
second writing path, it is the same one with a different actor.
"""
from __future__ import annotations

import pytest

from app.db.game_state_store import GameStateStore
from app.engines import npc_brain, npc_schedule
from app.engines.rules_engine import RulesEngine
from app.models.game import (
    EntityDef,
    EntityState,
    ExitDef,
    LocationDef,
    RoutineEntryDef,
    WorldDefinition,
)
from app.services import npc_turns
from app.services.action_service import ActionService
from app.services.world_service import bind_world, save_world


def _world() -> WorldDefinition:
    return WorldDefinition(
        world_id="town",
        name="Town",
        start_location_id="square",
        locations=[
            LocationDef(id="square", name="Square", exits=[ExitDef(target_id="forge")]),
            LocationDef(id="forge", name="Forge", exits=[ExitDef(target_id="square")]),
        ],
        entities=[
            EntityDef(
                id="smith",
                name="Smith",
                location_id="square",
                schedule=[
                    RoutineEntryDef(
                        start="09:00", activity="work", location_id="forge", minutes=480
                    )
                ],
                traits={"diligence": 0.95},
            )
        ],
    )


@pytest.fixture()
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    save_world(store, _world())
    yield store
    store.close()


@pytest.fixture()
def state(store):
    state = bind_world(store, "c1", "town", reset=True)
    # Routines need a runtime record: an entity that never changed has none.
    if "smith" not in state.entities:
        state.entities["smith"] = EntityState(entity_id="smith", location_id="square")
        from app.engines.game_clock import Clock

        store.commit_action(
            campaign_id="c1",
            request_id="seed",
            request_payload={"action": "custom"},
            status="applied",
            result_payload={},
            state=state.model_copy(update={"version": state.version + 1}),
            events=[],
            expected_version=state.version,
        )
        state = store.get_state("c1")
    return state


def _service(store) -> ActionService:
    return ActionService(store=store, rules=RulesEngine())


async def test_an_npc_turn_is_committed_by_the_same_writer_as_a_player_turn(store, state):
    service = _service(store)
    before = store.get_state("c1")
    results, failures = await npc_turns.run_round(service, "c1", limit=1)
    assert results, f"the smith should have gone to work: {failures}"
    after = store.get_state("c1")
    assert after.version > before.version
    # The actor really moved, and the write went through the ordinary action
    # pipeline - there is no NPC-only commit path.
    assert after.entities["smith"].location_id == "forge"
    assert results[0]["status"] == "applied"


async def test_an_npc_cannot_leak_into_the_player_position(store, state):
    service = _service(store)
    await npc_turns.run_round(service, "c1", limit=1)
    after = store.get_state("c1")
    assert after.location_id == "square"


async def test_an_unreachable_workplace_is_a_refusal_not_a_teleport(store, state):
    world = _world()
    world.locations[0].exits = []  # the forge is no longer reachable
    save_world(store, world)
    # Drive the decision directly: with the route gone the utility model may
    # reasonably prefer something else, and this test is about the *refusal*.
    decision = npc_brain.decide(
        store=store,
        campaign_id="c1",
        state=store.get_state("c1"),
        world=store.get_world("town"),
        npc=store.get_world("town").entities[0],
    )
    decision.intent = "work"
    decision.plan = [
        {"action": "travel", "target_id": "forge", "params": {"actor_id": "smith"}}
    ]
    original = npc_brain.decide
    npc_brain.decide = lambda **kwargs: decision
    try:
        service = _service(store)
        results, failures = await npc_turns.run_round(service, "c1", limit=1)
    finally:
        npc_brain.decide = original
    # The rules engine said no, and that answer is what got recorded: the NPC
    # did not silently teleport into the forge.
    assert len(results) == 1
    assert results[0]["status"] == "rejected"
    assert results[0]["code"] == "no_route"
    after = store.get_state("c1")
    assert after.entities["smith"].location_id == "square"


def test_routines_are_queued_once_per_slot(store, state):
    world = store.get_world("town")
    # The campaign starts at 08:00 and the smith's shift is 09:00, so today's
    # slot is still ahead; tomorrow's is queued too. A slot in the past is never
    # queued back in, so the count is "today + horizon", not "every slot".
    first = npc_turns.ensure_scheduled(store, "c1", world, state, horizon=1)
    assert first == 2  # today and tomorrow
    again = npc_turns.ensure_scheduled(store, "c1", world, state, horizon=1)
    assert again == 0, "re-queuing must not grow the table"


def test_the_routine_handler_repairs_a_need_without_writing_elsewhere(store, state):
    world = store.get_world("town")
    call = npc_turns.routine_handler(None)
    entry = npc_schedule.routine_entries(world, day=1)[0]
    snapshot = store.get_state("c1")
    new_state, events, scheduled = call(store, snapshot, entry)
    assert new_state.entities["smith"].activity == "work"
    assert events[0]["payload"]["npc_id"] == "smith"
    assert scheduled == []
    # The handler works on the copy the scheduler already owns: it commits
    # nothing by itself, exactly like every other tick handler.
    assert store.get_state("c1").version == snapshot.version
