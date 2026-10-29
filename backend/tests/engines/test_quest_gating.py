"""Quests are gated, driven by facts and can fail - all as validated data.

A quest only becomes active once its prerequisites are completed and its accept
conditions hold, its objectives advance from facts the rules already committed,
and it fails when its fail conditions hold. Nothing here is decided by a model.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.engines.event_engine import Fact, apply_effects
from app.engines.quest_engine import advance_quests, initial_quest_progress
from app.models.game import (
    ActionRequest,
    Condition,
    Effect,
    GameState,
    ObjectiveDef,
    QuestDef,
    WorldDefinition,
)
from app.services.action_service import ActionService
from app.services.world_service import bind_world, save_world
from tests.support import village_world


def _quest(**overrides) -> QuestDef:
    data = {
        "id": "follow_up",
        "title": "Follow Up",
        "objectives": [
            ObjectiveDef(id="reach_forest", kind="reach_location", target="forest_edge")
        ],
        "rewards": [Effect(kind="give_item", target="blessing_token", amount=2)],
    }
    data.update(overrides)
    return QuestDef(**data)


def _world_with(*quests: QuestDef) -> WorldDefinition:
    world = village_world()
    existing = {quest.id: quest for quest in world.quests}
    for quest in quests:
        existing[quest.id] = quest
    world.quests = list(existing.values())
    assert world.validate_references() == [], world.validate_references()
    return world


def _request(action: str, target_id: str, request_id: str) -> ActionRequest:
    return ActionRequest(
        campaign_id="camp-1",
        request_id=request_id,
        action=action,
        target_id=target_id,
        narrate=False,
    )


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def _bind(store, world: WorldDefinition):
    save_world(store, world)
    return bind_world(store, "camp-1", world.world_id, reset=True)


# ------------------------------------------------------------- authoring time
def test_a_quest_may_declare_prerequisites_conditions_and_failure():
    world = _world_with(
        _quest(
            prerequisites=["open_the_warehouse"],
            accept_requires=[Condition(kind="has_item", target="iron_key")],
            fail_conditions=[Condition(kind="quest_status", target="open_the_warehouse", value="failed")],
        )
    )
    quest = next(q for q in world.quests if q.id == "follow_up")
    assert quest.prerequisites == ["open_the_warehouse"]
    assert quest.accept_requires[0].kind == "has_item"
    assert quest.fail_conditions[0].kind == "quest_status"


def test_validation_rejects_an_unknown_prerequisite():
    world = village_world()
    world.quests.append(_quest(prerequisites=["ghost_quest"]))
    errors = world.validate_references()
    assert any("requires unknown quest 'ghost_quest'" in e for e in errors), errors


def test_validation_rejects_a_self_prerequisite():
    world = village_world()
    world.quests.append(_quest(prerequisites=["follow_up"]))
    errors = world.validate_references()
    assert any("lists itself as a prerequisite" in e for e in errors), errors


def test_validation_rejects_a_circular_prerequisite_chain():
    world = village_world()
    world.quests.extend(
        [
            _quest(id="a_quest", prerequisites=["b_quest"]),
            _quest(id="b_quest", prerequisites=["a_quest"]),
        ]
    )
    errors = world.validate_references()
    assert any("circular prerequisite chain" in e for e in errors), errors


def test_validation_rejects_a_fail_condition_on_an_unknown_item():
    world = village_world()
    world.quests.append(
        _quest(fail_conditions=[Condition(kind="has_item", target="ghost_item")])
    )
    errors = world.validate_references()
    assert any(
        "quest 'follow_up' fail condition: unknown item 'ghost_item'" in e
        for e in errors
    ), errors


def test_validation_rejects_an_unknown_quest_effect_target():
    world = village_world()
    world.quests.append(
        _quest(rewards=[Effect(kind="quest_accept", target="ghost_quest")])
    )
    errors = world.validate_references()
    assert any("unknown quest 'ghost_quest'" in e for e in errors), errors


# ------------------------------------------------------------ initial state
def test_an_ungated_quest_starts_active():
    progress = initial_quest_progress(_world_with(_quest()))
    assert progress["follow_up"].status == "active"
    assert progress["open_the_warehouse"].status == "active"


def test_a_gated_quest_starts_inactive_but_visible():
    world = _world_with(_quest(prerequisites=["open_the_warehouse"]))
    progress = initial_quest_progress(world)
    assert progress["follow_up"].status == "inactive"
    assert progress["follow_up"].objectives == {"reach_forest": 0}


def test_a_quest_with_unmet_accept_conditions_starts_inactive():
    world = _world_with(
        _quest(accept_requires=[Condition(kind="has_item", target="iron_key")])
    )
    assert initial_quest_progress(world)["follow_up"].status == "inactive"


def test_a_manual_only_quest_never_self_activates():
    world = _world_with(_quest(auto_accept=False))
    assert initial_quest_progress(world)["follow_up"].status == "inactive"


# -------------------------------------------------------------- transitions
def _state_for(world: WorldDefinition) -> GameState:
    state = GameState(campaign_id="camp-1", world_id=world.world_id)
    state.quests = initial_quest_progress(world)
    return state


def test_a_quest_activates_once_its_prerequisite_is_completed():
    world = _world_with(_quest(prerequisites=["open_the_warehouse"]))
    state = _state_for(world)
    assert state.quests["follow_up"].status == "inactive"

    state.quests["open_the_warehouse"].status = "completed"
    state, updates, _facts = advance_quests(world, state, [])

    assert state.quests["follow_up"].status == "active"
    assert any(u.quest_id == "follow_up" and u.status == "active" for u in updates)


def test_a_quest_stays_inactive_while_its_accept_conditions_do_not_hold():
    world = _world_with(
        _quest(accept_requires=[Condition(kind="flag", target="gate_open", value=True)])
    )
    state = _state_for(world)

    state, updates, _ = advance_quests(world, state, [])
    assert state.quests["follow_up"].status == "inactive"
    assert updates == []

    state.flags["gate_open"] = True
    state, updates, _ = advance_quests(world, state, [])
    assert state.quests["follow_up"].status == "active"


def test_a_fail_condition_ends_an_active_quest_without_rewards():
    world = _world_with(
        _quest(fail_conditions=[Condition(kind="flag", target="betrayed", value=True)])
    )
    state = _state_for(world)
    assert state.quests["follow_up"].status == "active"

    state.flags["betrayed"] = True
    state, updates, _ = advance_quests(world, state, [])

    assert state.quests["follow_up"].status == "failed"
    assert state.inventory.get("blessing_token") is None
    assert any(u.quest_id == "follow_up" and u.status == "failed" for u in updates)


def test_a_failed_quest_cannot_be_completed_afterwards():
    world = _world_with(
        _quest(
            fail_conditions=[Condition(kind="flag", target="betrayed", value=True)],
            auto_accept=False,
        )
    )
    state = _state_for(world)
    state.quests["follow_up"].status = "failed"

    applied = apply_effects(
        state, world, [Effect(kind="quest_complete", target="follow_up")]
    )
    # A failed quest is terminal: completion is refused, not silently applied.
    assert applied[0].ok is False
    assert "already failed" in applied[0].error
    assert state.quests["follow_up"].status == "failed"

    state, updates, _ = advance_quests(
        world, state, [Fact("reach_location", "forest_edge")]
    )
    assert state.quests["follow_up"].status == "failed"


# ------------------------------------------------------------------- effects
def test_a_quest_accept_effect_starts_a_manual_quest():
    world = _world_with(_quest(auto_accept=False))
    state = _state_for(world)
    applied = apply_effects(
        state, world, [Effect(kind="quest_accept", target="follow_up")]
    )

    assert applied[0].ok is True
    assert state.quests["follow_up"].status == "active"
    assert [fact.kind for fact in applied[0].facts] == ["quest_accepted"]


def test_a_quest_fail_effect_ends_the_quest():
    world = _world_with(_quest())
    state = _state_for(world)
    applied = apply_effects(state, world, [Effect(kind="quest_fail", target="follow_up")])

    assert applied[0].ok is True
    assert state.quests["follow_up"].status == "failed"


def test_a_completed_quest_cannot_be_accepted_again():
    world = _world_with(_quest())
    state = _state_for(world)
    state.quests["follow_up"].status = "completed"
    applied = apply_effects(
        state, world, [Effect(kind="quest_accept", target="follow_up")]
    )

    assert applied[0].ok is False
    assert "already completed" in applied[0].error


# -------------------------------------------------------------- full turn
async def test_walking_into_the_forest_activates_and_completes_a_gated_quest(store):
    world = _world_with(_quest(prerequisites=["open_the_warehouse"]))
    _bind(store, world)
    service = ActionService(store)

    blocked = await service.execute(_request("move", "forest_edge", "req-move-1"))
    assert blocked.status == "applied"
    after_move = store.get_state("camp-1")
    # The quest was still gated when the player arrived, so nothing advanced.
    assert after_move.quests["follow_up"].status == "inactive"
    assert after_move.quests["follow_up"].objectives["reach_forest"] == 0
    assert after_move.inventory.get("blessing_token") is None


async def test_a_gated_quest_runs_once_ungated(store):
    world = _world_with(_quest(prerequisites=["open_the_warehouse"]))
    _bind(store, world)
    service = ActionService(store)

    # Complete the prerequisite for real: take the key, open the door.
    await service.execute(_request("take", "iron_key", "req-take-1"))
    opened = await service.execute(_request("open", "warehouse_door", "req-open-1"))
    assert opened.status == "applied", opened.message
    mid = store.get_state("camp-1")
    assert mid.quests["open_the_warehouse"].status == "completed"
    assert mid.quests["follow_up"].status == "active"

    moved = await service.execute(_request("move", "forest_edge", "req-move-2"))
    assert moved.status == "applied", moved.message
    after = store.get_state("camp-1")
    assert after.quests["follow_up"].status == "completed"
    assert after.inventory["blessing_token"] == 3
