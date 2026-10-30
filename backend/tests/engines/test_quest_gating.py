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


@pytest.mark.parametrize("value, expected", [(True, 1), ("ready", 1), (False, 0), (None, 0), (0, 0)])
def test_flag_objectives_count_only_truthy_facts(value, expected):
    world = _world_with(_quest(objectives=[
        ObjectiveDef(id="flag", kind="flag", target="ready", required_count=2)
    ]))
    state, _, _ = advance_quests(world, _state_for(world), [Fact("flag", "ready", value)])
    assert state.quests["follow_up"].objectives["flag"] == expected


@pytest.mark.parametrize("amount, expected", [(3, 3), (None, 1), (0, 0), (-2, 0)])
def test_item_objectives_use_fact_quantities(amount, expected):
    world = _world_with(_quest(objectives=[
        ObjectiveDef(id="items", kind="obtain_item", target="iron_key", required_count=5)
    ]))
    state, _, _ = advance_quests(
        world, _state_for(world), [Fact("item_gained", "iron_key", amount)]
    )
    assert state.quests["follow_up"].objectives["items"] == expected


def test_failed_rewards_preserve_objectives_and_retry_atomically_without_new_facts():
    world = _world_with(_quest(rewards=[
        Effect(kind="give_item", target="blessing_token", amount=2),
        Effect(kind="set_flag", target="reward_granted", value=True),
        Effect(kind="remove_item", target="iron_key", amount=2),
    ]))
    original = _state_for(world)
    state, updates, facts = advance_quests(
        world, original, [Fact("reach_location", "forest_edge")]
    )
    assert original.quests["follow_up"].objectives["reach_forest"] == 0
    assert state.quests["follow_up"].status == "active"
    assert state.quests["follow_up"].objectives["reach_forest"] == 1
    assert state.inventory == {}
    assert state.flags == {}
    assert facts == []
    assert len(updates) == 1
    assert "reward failed" in updates[0].error
    assert updates[0].rewards_applied == []

    state.inventory["iron_key"] = 2
    state, updates, facts = advance_quests(world, state, [])
    assert state.quests["follow_up"].status == "completed"
    assert state.inventory == {"blessing_token": 2}
    assert state.flags["reward_granted"] is True
    assert updates[0].rewards_applied == world.quests[-1].rewards
    assert updates[0].error == ""
    assert Fact("item_gained", "blessing_token", 2) in facts
    assert Fact("quest_completed", "follow_up") in facts
    replayed, updates, facts = advance_quests(world, state, [])
    assert replayed == state
    assert updates == []
    assert facts == []


@pytest.mark.parametrize("fails", [False, True])
def test_explicit_completion_handles_cyclic_rewards_once_and_rolls_back_nested_failure(fails):
    first = _quest(rewards=[
        Effect(kind="give_item", target="blessing_token"),
        Effect(kind="quest_complete", target="second"),
    ])
    second = _quest(id="second", rewards=[
        Effect(kind="give_item", target="blessing_token", amount=2),
        Effect(kind="quest_complete", target="follow_up"),
    ])
    if fails:
        first.rewards.append(Effect(kind="remove_item", target="iron_key"))
    world = _world_with(first, second)
    state = _state_for(world)
    before = state.model_copy(deep=True)
    effect = Effect(kind="quest_complete", target="follow_up")
    result = apply_effects(state, world, [effect])[0]
    if fails:
        assert not result.ok
        assert "reward failed" in result.error
        assert result.facts == []
        assert state == before
    else:
        assert result.ok
        assert state.inventory["blessing_token"] == 3
        assert state.quests["follow_up"].status == "completed"
        assert state.quests["second"].status == "completed"
        assert result.facts.count(Fact("quest_completed", "follow_up")) == 1
        assert result.facts.count(Fact("quest_completed", "second")) == 1
        state, updates, _ = advance_quests(world, state, result.facts)
        assert {u.quest_id for u in updates if u.status == "completed"} == {"follow_up", "second"}
        again = apply_effects(state, world, [effect])[0]
        assert again.ok and again.facts == []
        assert state.inventory["blessing_token"] == 3


@pytest.mark.parametrize("blocked_by", ["disabled", "prerequisite", "condition", "failed", "completed"])
def test_manual_acceptance_cannot_bypass_gates_or_terminal_status(blocked_by):
    quest = _quest(auto_accept=False)
    if blocked_by == "disabled":
        quest.enabled = False
    elif blocked_by == "prerequisite":
        quest.prerequisites = ["open_the_warehouse"]
    elif blocked_by == "condition":
        quest.accept_requires = [Condition(kind="has_item", target="iron_key")]
    world = _world_with(quest)
    state = _state_for(world)
    if blocked_by in ("failed", "completed"):
        state.quests[quest.id].status = blocked_by
    before = state.model_copy(deep=True)
    result = apply_effects(state, world, [Effect(kind="quest_accept", target=quest.id)])[0]
    assert not result.ok
    assert result.error
    assert result.facts == []
    assert state == before


def test_manual_acceptance_succeeds_after_all_gates_hold_and_is_idempotent():
    world = _world_with(_quest(
        auto_accept=False,
        prerequisites=["open_the_warehouse"],
        accept_requires=[Condition(kind="has_item", target="iron_key")],
    ))
    state = _state_for(world)
    state.quests["open_the_warehouse"].status = "completed"
    state.inventory["iron_key"] = 1
    effect = Effect(kind="quest_accept", target="follow_up")
    accepted = apply_effects(state, world, [effect])[0]
    assert accepted.ok
    assert accepted.facts == [Fact("quest_accepted", "follow_up")]
    assert state.quests["follow_up"].status == "active"
    again = apply_effects(state, world, [effect])[0]
    assert again.ok and again.facts == []


@pytest.mark.parametrize("status", ["inactive", "failed", "completed"])
def test_explicit_progress_does_not_mutate_nonactive_quests(status):
    world = _world_with(_quest())
    state = _state_for(world)
    state.quests["follow_up"].status = status
    before = state.model_copy(deep=True)
    result = apply_effects(state, world, [Effect(
        kind="quest_progress", target="follow_up", value="reach_forest"
    )])[0]
    assert not result.ok
    assert result.facts == []
    assert state == before


def test_direct_progress_completion_grants_rewards_without_objective_facts():
    world = _world_with(_quest())
    state = _state_for(world)
    applied = apply_effects(state, world, [Effect(
        kind="quest_progress", target="follow_up", value="reach_forest"
    )])[0]
    state, updates, facts = advance_quests(world, state, applied.facts)
    assert state.quests["follow_up"].status == "completed"
    assert state.inventory["blessing_token"] == 2
    assert updates[0].status == "completed"
    assert Fact("item_gained", "blessing_token", 2) in facts


def test_quest_reward_chains_are_not_limited_to_three_passes():
    quests = [
        _quest(
            id=f"chain_{index}",
            objectives=[ObjectiveDef(id="ready", kind="flag", target=f"step_{index}")],
            rewards=[Effect(kind="set_flag", target=f"step_{index + 1}", value=True)],
        )
        for index in reversed(range(6))
    ]
    world = _world_with(*quests)
    state, updates, facts = advance_quests(
        world, _state_for(world), [Fact("flag", "step_0", True)]
    )
    assert all(state.quests[quest.id].status == "completed" for quest in quests)
    assert len(updates) == 6
    assert Fact("flag", "step_6", True) in facts
    assert len([fact for fact in facts if fact.kind == "flag"]) == 6


def test_progress_from_rewards_does_not_mask_another_quests_reward_failure():
    world = _world_with(
        _quest(id="source", rewards=[Effect(
            kind="quest_progress", target="follow_up", value="reach_forest"
        )]),
        _quest(rewards=[Effect(kind="remove_item", target="iron_key")]),
    )
    world.quests = [quest for quest in world.quests if quest.id != "open_the_warehouse"]
    world.quests.sort(key=lambda quest: quest.id != "source")
    state, updates, _ = advance_quests(
        world, _state_for(world), [Fact("reach_location", "forest_edge")]
    )
    failed_updates = [update for update in updates if update.quest_id == "follow_up"]
    assert state.quests["follow_up"].status == "active"
    assert len(failed_updates) == 1
    assert failed_updates[0].error
    assert failed_updates[0].rewards_applied == []
