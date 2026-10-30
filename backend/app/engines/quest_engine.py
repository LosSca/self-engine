"""Quest progression driven by confirmed facts.

A quest never completes because a model said so. Objectives advance only from
facts the rules and events already committed (an item was gained, a door was
opened, a location was reached). Rewards are ordinary effects, so they are
applied through the same validated path as everything else.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.engines.event_engine import AppliedEffect, Fact, apply_effects, condition_holds
from app.models.game import Effect, GameState, QuestProgress, WorldDefinition

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
    error: str = ""


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


def complete_quest(
    state: GameState, world: WorldDefinition, effect: Effect
) -> AppliedEffect:
    """Publish completion and all rewards together, including nested completions."""
    quest = next((q for q in world.quests if q.id == effect.target), None)
    if quest is None:
        return AppliedEffect(effect, False, f"unknown quest '{effect.target}'")
    if not quest.enabled:
        return AppliedEffect(effect, False, f"quest '{quest.id}' is disabled")
    progress = state.quests.get(quest.id)
    if progress is not None and progress.status == "completed":
        return AppliedEffect(effect, True)
    if progress is not None and progress.status == "failed":
        return AppliedEffect(effect, False, f"quest '{quest.id}' has already failed")
    if progress is None or progress.status != "active":
        return AppliedEffect(effect, False, f"quest '{quest.id}' is not active")
    if fail_conditions_met(state, world, quest):
        return AppliedEffect(effect, False, f"quest '{quest.id}' meets a fail condition")

    scratch = state.model_copy(deep=True)
    # Terminal on scratch only: self/cyclic rewards cannot grant twice.
    scratch.quests[quest.id].status = "completed"
    applied = apply_effects(scratch, world, quest.rewards)
    failed = next((item for item in applied if not item.ok), None)
    if failed is not None:
        return AppliedEffect(
            effect, False, f"quest '{quest.id}' reward failed: {failed.error}"
        )
    for name in GameState.model_fields:
        setattr(state, name, getattr(scratch, name))
    return AppliedEffect(
        effect,
        True,
        facts=[Fact("quest_completed", quest.id)]
        + [fact for item in applied for fact in item.facts],
    )


def _record_quest_facts(
    world: WorldDefinition,
    state: GameState,
    facts: List[Fact],
    updates: Dict[str, QuestUpdate],
) -> None:
    quests = {quest.id: quest for quest in world.quests}
    for fact in facts:
        if fact.kind not in (
            "quest_completed", "quest_accepted", "quest_failed", "quest_progress"
        ):
            continue
        quest_id = fact.target.split(":", 1)[0] if fact.kind == "quest_progress" else fact.target
        quest = quests.get(quest_id)
        progress = state.quests.get(quest_id)
        if quest is None or progress is None:
            continue
        updates[quest_id] = QuestUpdate(
            quest_id=quest_id,
            status=progress.status,
            objectives=dict(progress.objectives),
            rewards_applied=list(quest.rewards) if progress.status == "completed" else [],
        )


def advance_quests(
    world: WorldDefinition,
    state: GameState,
    facts: List[Fact],
    attempted: Optional[set[str]] = None,
) -> Tuple[GameState, List[QuestUpdate], List[Fact]]:
    """Consume each fact once; return all new quest/reward facts for events.

    Returned facts have already advanced quests. Share ``attempted`` across an
    action to defer failed rewards to the next accepted action.
    """
    state = state.model_copy(deep=True)
    updates: Dict[str, QuestUpdate] = {}
    diagnostics: List[QuestUpdate] = []
    attempted = attempted if attempted is not None else set()
    pending = list(facts)
    reward_facts: List[Fact] = []
    _record_quest_facts(world, state, pending, updates)

    for _ in range(len(world.quests) + 1):
        generated: List[Fact] = []
        state = _activate_gated_quests(world, state, updates, generated)
        state = _fail_doomed_quests(world, state, updates, generated)
        for quest in world.quests:
            if not quest.enabled:
                continue
            progress = state.quests.get(quest.id)
            if progress is None or progress.status != "active":
                continue
            if fail_conditions_met(state, world, quest):
                progress.status = "failed"
                generated.append(Fact("quest_failed", quest.id))
                continue
            changed = False
            for objective in quest.objectives:
                fact_kind = _FACT_FOR_OBJECTIVE.get(objective.kind)
                delta = 0
                for fact in pending:
                    if fact.kind != fact_kind or fact.target != objective.target:
                        continue
                    if objective.kind == "flag" and not fact.value:
                        continue
                    if objective.kind == "obtain_item" and fact.value is not None:
                        if not isinstance(fact.value, int) or isinstance(fact.value, bool):
                            continue
                        delta += max(0, fact.value)
                    else:
                        delta += 1
                current = progress.objectives.get(objective.id, 0)
                updated = min(objective.required_count, current + delta)
                if updated != current:
                    progress.objectives[objective.id] = updated
                    changed = True
            if changed:
                updates[quest.id] = QuestUpdate(
                    quest_id=quest.id,
                    status=progress.status,
                    objectives=dict(progress.objectives),
                )
            complete = bool(quest.objectives) and all(
                progress.objectives.get(objective.id, 0) >= objective.required_count
                for objective in quest.objectives
            )
            if complete and quest.id not in attempted:
                attempted.add(quest.id)
                applied = complete_quest(
                    state, world, Effect(kind="quest_complete", target=quest.id)
                )
                if applied.ok:
                    generated.extend(applied.facts)
                else:
                    updates.pop(quest.id, None)
                    diagnostics.append(QuestUpdate(
                        quest_id=quest.id,
                        status=progress.status,
                        objectives=dict(progress.objectives),
                        error=applied.error,
                    ))
        _record_quest_facts(world, state, generated, updates)
        reward_facts.extend(generated)
        pending = generated
        if not pending:
            break

    for diagnostic in diagnostics:
        update = updates.get(diagnostic.quest_id)
        if update is not None and update.status == "active":
            updates.pop(diagnostic.quest_id)
    return state, list(updates.values()) + diagnostics, reward_facts


def _activate_gated_quests(
    world: WorldDefinition,
    state: GameState,
    updates: Dict[str, QuestUpdate],
    generated: List[Fact],
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
        generated.append(Fact("quest_accepted", quest.id))
        updates[quest.id] = QuestUpdate(
            quest_id=quest.id,
            status="active",
            objectives=dict(progress.objectives),
        )
    return state


def _fail_doomed_quests(
    world: WorldDefinition,
    state: GameState,
    updates: Dict[str, QuestUpdate],
    generated: List[Fact],
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
        generated.append(Fact("quest_failed", quest.id))
        updates[quest.id] = QuestUpdate(
            quest_id=quest.id,
            status="failed",
            objectives=dict(progress.objectives),
        )
    return state
