"""Quest progression driven by confirmed facts.

A quest never completes because a model said so. Objectives advance only from
facts the rules and events already committed (an item was gained, a door was
opened, a location was reached). Rewards are ordinary effects, so they are
applied through the same validated path as everything else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from app.engines.event_engine import Fact, apply_effects
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


def initial_quest_progress(world: WorldDefinition) -> Dict[str, QuestProgress]:
    """Every enabled quest starts active with zeroed objectives."""
    progress: Dict[str, QuestProgress] = {}
    for quest in world.quests:
        if not quest.enabled:
            continue
        progress[quest.id] = QuestProgress(
            quest_id=quest.id,
            status="active",
            objectives={objective.id: 0 for objective in quest.objectives},
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
