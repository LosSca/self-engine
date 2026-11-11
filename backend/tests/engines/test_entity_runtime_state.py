"""Run-time entity state: NPCs can move, die and stay dead across a reload.

The world document says where an entity *starts*; these tests pin down that the
campaign state says where it *is*, that events can move entities, and that an
old save without the new `entities` key still loads and behaves as before.
"""
from __future__ import annotations

import pytest

from app.db.game_state_store import GameStateStore
from app.engines.event_engine import condition_holds, entity_location
from app.engines.rules_engine import RulesEngine
from app.models.game import (
    Condition,
    Effect,
    EntityDef,
    ExitDef,
    GameEventDef,
    GameState,
    LocationDef,
    WorldDefinition,
)
from app.services.action_service import ActionService
from app.services.narrative_service import TemplateNarrator
from app.services.world_service import bind_world, save_world


def two_room_world(entity_location_id: str = "square") -> WorldDefinition:
    return WorldDefinition(
        world_id="entity_world",
        name="Two Rooms",
        start_location_id="square",
        locations=[
            LocationDef(
                id="square",
                name="Square",
                description="A wide square.",
                exits=[ExitDef(target_id="hall", label="east")],
            ),
            LocationDef(
                id="hall",
                name="Hall",
                description="A long hall.",
                exits=[ExitDef(target_id="square", label="west")],
            ),
        ],
        entities=[
            EntityDef(
                id="guard",
                name="Guard",
                description="A watchful guard.",
                location_id=entity_location_id,
                kind="npc",
                dialogue=["Halt. State your business."],
            ),
            EntityDef(
                id="wolf",
                name="Wolf",
                location_id="hall",
                kind="creature",
                hostile=True,
                attributes={"max_hp": 6, "attack": 4, "defense": 1},
            ),
        ],
        events=[
            GameEventDef(
                id="guard_patrols",
                name="The guard walks his round",
                trigger="action",
                trigger_action="look",
                conditions=[Condition(kind="entity_at", target="guard", value="square")],
                effects=[Effect(kind="move_entity", target="guard", value="hall")],
                repeat="always",
            )
        ],
    )


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    s = GameStateStore(str(tmp_path / "state.db"))
    yield s
    s.close()


def test_a_successful_turn_materialises_the_entities_present(store):
    world = two_room_world()
    save_world(store, world)
    state = bind_world(store, "camp-1", world.world_id, reset=True)
    assert state.entities == {}

    result = RulesEngine().evaluate(world, state, "look")

    assert result.ok
    assert [effect.kind for effect in result.effects] == ["seed_entity"]
    assert result.effects[0].target == "guard"
    assert "People here: Guard." in result.message


def test_entities_elsewhere_are_not_materialised(store):
    world = two_room_world(entity_location_id="hall")
    save_world(store, world)
    state = bind_world(store, "camp-1", world.world_id, reset=True)

    result = RulesEngine().evaluate(world, state, "look")

    assert result.ok
    assert result.effects == []
    assert "People here" not in result.message


def test_moving_the_player_changes_who_is_present(store):
    world = two_room_world()
    save_world(store, world)
    state = bind_world(store, "camp-2", world.world_id, reset=True)
    rules = RulesEngine()

    from app.engines.event_engine import apply_effects

    apply_effects(state, world, rules.evaluate(world, state, "look").effects)
    moved = rules.evaluate(world, state, "move", "hall")
    assert moved.ok
    apply_effects(state, world, moved.effects)

    # The guard starts in the square: from the hall the rules say nobody is
    # there to talk to (only the wolf, which is hostile but talkable).
    assert rules.evaluate(world, state, "talk", "guard").code == "entity_not_here"


def test_a_moved_entity_is_talkable_where_it_went(store):
    world = two_room_world()
    save_world(store, world)
    state = bind_world(store, "camp-3", world.world_id, reset=True)
    rules = RulesEngine()

    from app.engines.event_engine import apply_effects

    apply_effects(state, world, [Effect(kind="move_entity", target="guard", value="hall")])
    state.location_id = "hall"

    assert entity_location(state, world, "guard") == "hall"
    talk = rules.evaluate(world, state, "talk", "guard")
    assert talk.code == "talked"
    assert "Halt. State your business." in talk.message


def test_a_dead_entity_cannot_be_talked_to(store):
    world = two_room_world(entity_location_id="square")
    save_world(store, world)
    state = bind_world(store, "camp-4", world.world_id, reset=True)
    rules = RulesEngine()
    from app.engines.event_engine import apply_effects

    apply_effects(state, world, [Effect(kind="combat_hp", target="guard", value=0)])

    assert state.entities["guard"].alive is False
    assert condition_holds(state, world, Condition(kind="entity_alive", target="guard")) is False
    assert rules.evaluate(world, state, "talk", "guard").code == "entity_dead"


def test_the_runtime_position_beats_the_authored_one(store):
    world = two_room_world()
    save_world(store, world)
    state = bind_world(store, "camp-5", world.world_id, reset=True)
    from app.engines.event_engine import apply_effects

    apply_effects(state, world, [Effect(kind="move_entity", target="guard", value="hall")])
    state.location_id = "square"

    assert entity_location(state, world, "guard") == "hall"
    assert "People here" not in RulesEngine().evaluate(world, state, "look").message
    assert condition_holds(
        state, world, Condition(kind="entity_at", target="guard", value="square")
    ) is False
    assert condition_holds(
        state, world, Condition(kind="entity_at", target="guard", value="hall")
    ) is True


def test_seeding_is_idempotent(store):
    world = two_room_world()
    save_world(store, world)
    state = bind_world(store, "camp-6", world.world_id, reset=True)
    rules = RulesEngine()
    from app.engines.event_engine import apply_effects

    first = rules.evaluate(world, state, "look")
    apply_effects(state, world, first.effects)
    second = rules.evaluate(world, state, "look")

    assert second.effects == []
    # Applying a seed twice is harmless: the record already exists.
    results = apply_effects(state, world, first.effects)
    assert all(result.ok for result in results)


def test_unreachable_entities_are_never_reported_as_present(store):
    world = two_room_world(entity_location_id="hall")
    save_world(store, world)
    state = bind_world(store, "camp-7", world.world_id, reset=True)

    message = RulesEngine().evaluate(world, state, "look").message

    assert "Guard" not in message
    assert "Wolf" not in message


async def test_an_event_moves_an_entity_and_the_state_version_advances(store):
    world = two_room_world()
    save_world(store, world)
    bind_world(store, "camp-8", world.world_id, reset=True)
    service = ActionService(store, narrator=TemplateNarrator())

    from app.models.game import ActionRequest

    result = await service.execute(
        ActionRequest(campaign_id="camp-8", request_id="r1", action="look")
    )
    assert result.status == "applied"

    reloaded = store.get_state("camp-8")
    assert reloaded.version == 1
    # The look seeded the guard, then the patrol event moved him.
    assert reloaded.entities["guard"].location_id == "hall"


async def test_a_replayed_request_does_not_move_an_entity_twice(store):
    world = two_room_world()
    save_world(store, world)
    bind_world(store, "camp-9", world.world_id, reset=True)
    service = ActionService(store, narrator=TemplateNarrator())

    from app.models.game import ActionRequest

    request = ActionRequest(campaign_id="camp-9", request_id="same", action="look")
    first = await service.execute(request)
    second = await service.execute(request)

    assert first.status == "applied"
    assert second.status == "duplicate"
    state = store.get_state("camp-9")
    assert state.version == 1
    assert state.entities["guard"].location_id == "hall"


def test_an_old_save_without_entity_records_still_loads_and_plays(store):
    world = two_room_world()
    save_world(store, world)
    bind_world(store, "camp-10", world.world_id, reset=True)

    # A payload written before this feature existed has no 'entities' key.
    raw = store.get_state("camp-10").model_dump(mode="json")
    raw.pop("entities", None)
    legacy = GameState.model_validate(raw)

    assert legacy.entities == {}
    look = RulesEngine().evaluate(world, legacy, "look")
    assert look.ok
    assert entity_location(legacy, world, "guard") == "square"


def test_world_validation_rejects_unknown_entity_and_location():
    world = two_room_world()
    world.events[0] = GameEventDef(
        id="bad",
        name="Bad event",
        trigger="action",
        effects=[Effect(kind="move_entity", target="ghost", value="nowhere")],
    )
    errors = world.validate_references()
    assert "event 'bad' effect: unknown entity 'ghost'" in errors
    assert "event 'bad' effect: unknown location 'nowhere'" in errors


def test_entity_at_requires_a_known_location():
    world = two_room_world()
    world.events[0] = GameEventDef(
        id="bad2",
        name="Bad condition",
        trigger="action",
        conditions=[Condition(kind="entity_at", target="guard", value="moon")],
    )
    errors = world.validate_references()
    assert "event 'bad2' condition: unknown location 'moon'" in errors
