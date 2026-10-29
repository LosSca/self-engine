"""Quest progression driven by confirmed facts.

A quest never completes because a model said so. Objectives advance only from
facts the rules and events already committed (an item was gained, a door was
opened, a location was reached). Rewards are ordinary effects, so they are
applied through the same validated path as everything else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from app.engines.event_engine import Fact, apply_effects, condition_holds
from app.models.game import Effect, GameState, QuestProgress, WorldDefinition

MAX_QUEST_PASSES = 3

_FACT_FOR_OBJECTIVE = {
    "obtain_item": "item_gained",
    "open_door": "door_opened",
    "reach_location": "reach_location",
    "flag": "flag",
}


@dataclass
class QuestUpdate:
    quest_id: str
    status: str
    objectives: Dict[str, int] = field(default_factory=dict)
    rewards_applied: List[Effect] = field(default_factory=list)


def _zeroed(quest) -> Dict[str, int]:
    return {objective.id: 0 for objective in quest.objectives}


def prerequisites_met(state: GameState, quest) -> bool:
    """A quest may only start once every quest it names is completed."""
    for prerequisite in quest.prerequisites:
        progress = state.quests.get(prerequisite)
        if progress is None or progress.status != "completed":
            return False
    return True


def accept_conditions_met(state: GameState, world: WorldDefinition, quest) -> bool:
    return all(condition_holds(state, world, c) for c in quest.accept_requires)


def fail_conditions_met(state: GameState, world: WorldDefinition, quest) -> bool:
    """Any single fail condition ending the quest is enough."""
    return any(condition_holds(state, world, c) for c in quest.fail_conditions)


def initial_quest_progress(world: WorldDefinition) -> Dict[str, QuestProgress]:
    """Enabled quests start active unless gated by a prerequisite or condition.

    A gated quest is still persisted, as `inactive`, so the player can see it
    waiting; it becomes active later through the same activation path a manual
    `quest_accept` effect uses.
    """
    state = GameState(campaign_id="_initial", world_id=world.world_id)
    progress: Dict[str, QuestProgress] = {}
    for quest in world.quests:
        if not quest.enabled:
            continue
        active = (
            quest.auto_accept
            and prerequisites_met(state, quest)
            and accept_conditions_met(state, world, quest)
        )
        progress[quest.id] = QuestProgress(
            quest_id=quest.id,
            status="active" if active else "inactive",
            objectives=_zeroed(quest),
        )
    return progress


def advance_quests(
    world: WorldDefinition,
    state: GameState,
    facts: List[Fact],
) -> Tuple[GameState, List[QuestUpdate], List[Fact]]:
    """Advance objectives from facts, complete finished quests, grant rewards."""
    state = state.model_copy(deep=True)
    updates: Dict[str, QuestUpdate] = {}
    pending = list(facts)
    reward_facts: List[Fact] = []

    for _ in range(MAX_QUEST_PASSES):
        state = _activate_gated_quests(world, state, updates)
        state = _fail_doomed_quests(world, state, updates)
        if not pending:
            break
        progressed = False
        for quest in world.quests:
            if not quest.enabled:
                continue
            progress = state.quests.get(quest.id)
            if progress is None or progress.status != "active":
                continue
            changed = False
            for objective in quest.objectives:
                fact_kind = _FACT_FOR_OBJECTIVE.get(objective.kind)
                if fact_kind is None:
                    continue
                delta = 0
                for fact in pending:
                    if fact.kind != fact_kind or fact.target != objective.target:
                        continue
                    if objective.kind == "flag" and fact.value is not None:
                        continue
                    delta += 1
                if delta <= 0:
                    continue
                current = progress.objectives.get(objective.id, 0)
                updated = min(objective.required_count, current + delta)
                if updated != current:
                    progress.objectives[objective.id] = updated
                    changed = True
                    progressed = True
            state.quests[quest.id] = progress
            complete = bool(quest.objectives) and all(
                progress.objectives.get(objective.id, 0) >= objective.required_count
                for objective in quest.objectives
            )
            if complete:
                progress.status = "completed"
                applied = apply_effects(state, world, quest.rewards)
                for item in applied:
                    reward_facts.extend(item.facts)
                updates[quest.id] = QuestUpdate(
                    quest_id=quest.id,
                    status="completed",
                    objectives=dict(progress.objectives),
                    rewards_applied=list(quest.rewards),
                )
            elif changed:
                updates[quest.id] = QuestUpdate(
                    quest_id=quest.id,
                    status=progress.status,
                    objectives=dict(progress.objectives),
                )
        pending = reward_facts
        reward_facts = []
        if not progressed:
            break

    return state, list(updates.values()), reward_facts


def _activate_gated_quests(
    world: WorldDefinition, state: GameState, updates: Dict[str, QuestUpdate]
) -> GameState:
    """Start every inactive quest whose prerequisites and conditions now hold."""
    for quest in world.quests:
        if not quest.enabled or not quest.auto_accept:
            continue
        progress = state.quests.get(quest.id)
        if progress is None or progress.status != "inactive":
            continue
        if not prerequisites_met(state, quest):
            continue
        if not accept_conditions_met(state, world, quest):
            continue
        progress.status = "active"
        for objective in quest.objectives:
            progress.objectives.setdefault(objective.id, 0)
        state.quests[quest.id] = progress
        updates[quest.id] = QuestUpdate(
            quest_id=quest.id,
            status="active",
            objectives=dict(progress.objectives),
        )
    return state


def _fail_doomed_quests(
    world: WorldDefinition, state: GameState, updates: Dict[str, QuestUpdate]
) -> GameState:
    """End active quests whose fail conditions hold. Rewards are not granted."""
    for quest in world.quests:
        if not quest.enabled or not quest.fail_conditions:
            continue
        progress = state.quests.get(quest.id)
        if progress is None or progress.status != "active":
            continue
        if not fail_conditions_met(state, world, quest):
            continue
        progress.status = "failed"
        state.quests[quest.id] = progress
        updates[quest.id] = QuestUpdate(
            quest_id=quest.id,
            status="failed",
            objectives=dict(progress.objectives),
        )
    return state
