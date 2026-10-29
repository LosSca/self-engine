"""Structured event engine.

Events are data: a trigger, validated conditions and validated effects. There
is no scripting, no eval and no arbitrary code path. An event whose data cannot
be resolved against the world fails closed with a readable reason and leaves the
authoritative state untouched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.models.game import (
    CombatantState,
    Condition,
    Effect,
    EventExecution,
    GameEventDef,
    GameState,
    QuestProgress,
    WorldDefinition,
)

MAX_CASCADE_PASSES = 2


@dataclass(frozen=True)
class Fact:
    """A confirmed state change, used to drive quests and later events."""

    kind: str
    target: str
    value: Any = None


@dataclass
class AppliedEffect:
    effect: Effect
    ok: bool
    error: str = ""
    facts: List[Fact] = field(default_factory=list)


def _compare(actual: Any, expected: Any, op: str) -> bool:
    if op in ("gte", "lte", "gt", "lt"):
        try:
            left = float(actual)
            right = float(expected)
        except (TypeError, ValueError):
            return False
        return {
            "gte": left >= right,
            "lte": left <= right,
            "gt": left > right,
            "lt": left < right,
        }[op]
    if op == "ne":
        return actual != expected
    return actual == expected


def _door_is_open(state: GameState, world: WorldDefinition, door_id: str) -> bool:
    door = next((d for d in world.doors if d.id == door_id), None)
    if door is None:
        return False
    return state.doors.get(door_id, door.opened)


def condition_holds(
    state: GameState, world: WorldDefinition, condition: Condition
) -> bool:
    kind = condition.kind
    if kind == "flag":
        return _compare(state.flags.get(condition.target), condition.value, condition.op)
    if kind == "has_item":
        held = state.inventory.get(condition.target, 0) > 0
        expected = True if condition.value is None else bool(condition.value)
        return _compare(held, expected, condition.op)
    if kind == "item_count":
        return _compare(
            state.inventory.get(condition.target, 0),
            condition.value if condition.value is not None else 1,
            condition.op,
        )
    if kind == "door_open":
        expected = True if condition.value is None else bool(condition.value)
        return _door_is_open(state, world, condition.target) == expected
    if kind == "at_location":
        return state.location_id == condition.target
    if kind == "quest_status":
        progress = state.quests.get(condition.target)
        status = progress.status if progress else "inactive"
        return _compare(status, condition.value, condition.op)
    if kind == "combatant_hp":
        combatant = state.combatants.get(condition.target)
        hp = combatant.hp if combatant else 0
        return _compare(hp, condition.value if condition.value is not None else 0, condition.op)
    if kind == "combatant_alive":
        combatant = state.combatants.get(condition.target)
        alive = bool(combatant and combatant.alive)
        expected = True if condition.value is None else bool(condition.value)
        return _compare(alive, expected, condition.op)
    return False


def apply_effect(
    state: GameState, world: WorldDefinition, effect: Effect
) -> AppliedEffect:
    """Apply one validated effect to a *copy* of the state.

    Returns an AppliedEffect whose ok=False carries a readable error; callers
    must not commit a partially applied effect list.
    """
    kind = effect.kind
    if kind == "set_flag":
        if not effect.target:
            return AppliedEffect(effect, False, "set_flag needs a target flag name")
        state.flags[effect.target] = effect.value
        return AppliedEffect(effect, True, facts=[Fact("flag", effect.target, effect.value)])

    if kind == "give_item":
        if not any(item.id == effect.target for item in world.items):
            return AppliedEffect(effect, False, f"unknown item '{effect.target}'")
        amount = max(1, int(effect.amount or 1))
        state.inventory[effect.target] = state.inventory.get(effect.target, 0) + amount
        state.item_locations.pop(effect.target, None)
        return AppliedEffect(
            effect, True, facts=[Fact("item_gained", effect.target, amount)]
        )

    if kind == "remove_item":
        amount = max(1, int(effect.amount or 1))
        held = state.inventory.get(effect.target, 0)
        if held < amount:
            return AppliedEffect(
                effect,
                False,
                f"cannot remove {amount} x '{effect.target}': only {held} held",
            )
        remaining = held - amount
        if remaining > 0:
            state.inventory[effect.target] = remaining
        else:
            state.inventory.pop(effect.target, None)
        return AppliedEffect(
            effect, True, facts=[Fact("item_removed", effect.target, amount)]
        )

    if kind == "move_player":
        if not any(loc.id == effect.target for loc in world.locations):
            return AppliedEffect(effect, False, f"unknown location '{effect.target}'")
        state.location_id = effect.target
        return AppliedEffect(
            effect, True, facts=[Fact("reach_location", effect.target)]
        )

    if kind == "open_door":
        if not any(door.id == effect.target for door in world.doors):
            return AppliedEffect(effect, False, f"unknown door '{effect.target}'")
        state.doors[effect.target] = True
        return AppliedEffect(effect, True, facts=[Fact("door_opened", effect.target)])

    if kind == "close_door":
        if not any(door.id == effect.target for door in world.doors):
            return AppliedEffect(effect, False, f"unknown door '{effect.target}'")
        state.doors[effect.target] = False
        return AppliedEffect(effect, True, facts=[Fact("door_closed", effect.target)])

    if kind == "quest_progress":
        quest = next((q for q in world.quests if q.id == effect.target), None)
        if quest is None:
            return AppliedEffect(effect, False, f"unknown quest '{effect.target}'")
        objective_id = effect.value
        objective = next(
            (o for o in quest.objectives if o.id == objective_id), None
        )
        if objective is None:
            return AppliedEffect(
                effect,
                False,
                f"quest '{quest.id}' has no objective '{objective_id}'",
            )
        progress = state.quests.get(quest.id) or QuestProgress(quest_id=quest.id)
        current = progress.objectives.get(objective.id, 0)
        progress.objectives[objective.id] = min(
            objective.required_count, current + max(1, int(effect.amount or 1))
        )
        state.quests[quest.id] = progress
        return AppliedEffect(
            effect,
            True,
            facts=[Fact("quest_progress", f"{quest.id}:{objective.id}")],
        )

    if kind == "quest_complete":
        quest = next((q for q in world.quests if q.id == effect.target), None)
        if quest is None:
            return AppliedEffect(effect, False, f"unknown quest '{effect.target}'")
        progress = state.quests.get(quest.id) or QuestProgress(quest_id=quest.id)
        progress.status = "completed"
        state.quests[quest.id] = progress
        return AppliedEffect(effect, True, facts=[Fact("quest_completed", quest.id)])

    if kind == "combat_hp":
        entity = next((entry for entry in world.entities if entry.id == effect.target), None)
        if entity is None:
            return AppliedEffect(effect, False, f"unknown entity '{effect.target}'")
        combatant = state.combatants.get(entity.id)
        if combatant is None:
            # Seed the same way the rules engine does, so authored attributes
            # (max_hp/attack/defense) are the single source of enemy numbers.
            def _number(key: str, default: float) -> float:
                value = entity.attributes.get(key, default)
                return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

            max_hp = max(1, int(_number("max_hp", _number("hp", 10))))
            combatant = CombatantState(
                entity_id=entity.id,
                name=entity.name,
                hp=max_hp,
                max_hp=max_hp,
                attack=max(1, int(_number("attack", 3))),
                defense=max(0, int(_number("defense", 1))),
                flee_chance=min(1.0, max(0.0, _number("flee_chance", 0.5))),
            )
        hp = 0 if effect.value is None else int(effect.value)
        combatant.hp = max(0, min(hp, combatant.max_hp))
        was_alive = combatant.alive
        combatant.alive = combatant.hp > 0
        state.combatants[entity.id] = combatant
        facts = [Fact("combat_hp", entity.id, combatant.hp)]
        if was_alive and not combatant.alive:
            facts.append(Fact("combatant_defeated", entity.id))
            state.combat["active"] = False
            state.flags["combat_active"] = False
        return AppliedEffect(effect, True, facts=facts)

    if kind == "player_damage":
        amount = max(1, int(effect.amount or 1))
        current = state.flags.get("player_hp")
        if not isinstance(current, int):
            current = int(state.flags.get("player_max_hp", 20) or 20)
        remaining = max(0, current - amount)
        state.flags["player_hp"] = remaining
        facts = [Fact("player_damage", "player", amount)]
        if remaining <= 0 and not state.flags.get("player_defeated"):
            state.flags["player_defeated"] = True
            facts.append(Fact("player_defeated", "player", 0))
        return AppliedEffect(effect, True, facts=facts)

    if kind == "emit":
        return AppliedEffect(
            effect, True, facts=[Fact("emit", effect.target, effect.value)]
        )

    return AppliedEffect(effect, False, f"unsupported effect kind '{kind}'")


def apply_effects(
    state: GameState, world: WorldDefinition, effects: List[Effect]
) -> List[AppliedEffect]:
    return [apply_effect(state, world, effect) for effect in effects]


def event_matches(event: GameEventDef, action: str, target_id: Optional[str]) -> bool:
    if not event.enabled:
        return False
    if event.trigger == "state_change":
        return True
    if event.trigger != "action":
        return False
    if event.trigger_action and event.trigger_action != action:
        return False
    if event.trigger_target and event.trigger_target != target_id:
        return False
    return True


def run_triggered_events(
    world: WorldDefinition,
    state: GameState,
    action: str,
    target_id: Optional[str],
    facts: List[Fact],
) -> Tuple[GameState, List[EventExecution], List[Fact]]:
    """Fire every matching event once, in priority order.

    Each event is applied atomically: effects go onto a scratch copy of the
    state, and a failing effect discards that copy and records a failure with a
    readable reason. Returns (new_state, executions, new_facts).
    """
    state = state.model_copy(deep=True)
    executions: List[EventExecution] = []
    new_facts: List[Fact] = []
    ordered = sorted(world.events, key=lambda e: (-e.priority, e.id))

    for event in ordered:
        if not event_matches(event, action, target_id):
            continue
        if event.repeat == "once" and event.id in state.fired_events:
            executions.append(
                EventExecution(
                    event_id=event.id,
                    name=event.name,
                    status="skipped",
                    reason="already fired (repeat=once)",
                )
            )
            continue
        if not all(condition_holds(state, world, c) for c in event.conditions):
            continue
        scratch = state.model_copy(deep=True)
        applied = apply_effects(scratch, world, event.effects)
        failed = [a for a in applied if not a.ok]
        if failed:
            executions.append(
                EventExecution(
                    event_id=event.id,
                    name=event.name,
                    status="failed",
                    reason=failed[0].error,
                    effects=event.effects,
                )
            )
            continue
        state = scratch
        if event.repeat == "once" and event.id not in state.fired_events:
            state.fired_events.append(event.id)
        for item in applied:
            new_facts.extend(item.facts)
        executions.append(
            EventExecution(
                event_id=event.id,
                name=event.name,
                status="fired",
                effects=event.effects,
            )
        )
    return state, executions, new_facts


def validate_event(world: WorldDefinition, event: GameEventDef) -> List[str]:
    """Semantic validation with readable messages (empty list == valid)."""
    errors: List[str] = []
    item_ids = {item.id for item in world.items}
    door_ids = {door.id for door in world.doors}
    location_ids = {loc.id for loc in world.locations}
    quest_ids = {quest.id for quest in world.quests}
    entity_ids = {entity.id for entity in world.entities}
    for condition in event.conditions:
        errors.extend(
            WorldDefinition._validate_condition(
                condition, item_ids, door_ids, location_ids, quest_ids, entity_ids
            )
        )
    for effect in event.effects:
        errors.extend(
            WorldDefinition._validate_effect(
                effect, item_ids, door_ids, quest_ids, entity_ids
            )
        )
    if event.trigger == "action" and not event.trigger_action:
        errors.append("trigger 'action' requires trigger_action")
    return errors


def simulate_event(
    world: WorldDefinition, state: GameState, event_id: str
) -> Dict[str, Any]:
    """Preview one event without touching the stored state."""
    event = next((e for e in world.events if e.id == event_id), None)
    if event is None:
        return {"status": "not_found", "error": f"unknown event '{event_id}'"}
    errors = validate_event(world, event)
    conditions_met = all(condition_holds(state, world, c) for c in event.conditions)
    scratch = state
    applied: List[AppliedEffect] = []
    failed: List[AppliedEffect] = []
    if errors:
        status = "invalid"
    elif not event.enabled:
        status = "disabled"
    elif not conditions_met:
        status = "not_applicable"
    else:
        scratch = state.model_copy(deep=True)
        applied = apply_effects(scratch, world, event.effects)
        failed = [item for item in applied if not item.ok]
        status = "failed" if failed else "would_fire"
        if failed:
            # Effects commit per event, atomically: a failure means nothing lands.
            scratch = state
    return {
        "status": status,
        "event_id": event.id,
        "name": event.name,
        "errors": errors,
        "conditions_met": conditions_met,
        "effects_applied": [item.effect.model_dump() for item in applied if item.ok],
        "errors_effect": [item.error for item in failed],
        "state_before": state.model_dump(),
        "state_after": scratch.model_dump(),
        "diff": diff_states(state, scratch),
    }


def diff_states(before: GameState, after: GameState) -> Dict[str, Any]:
    """Field-level before/after comparison, used by debug views and tests."""
    changed: Dict[str, Any] = {}
    for field_name in (
        "location_id",
        "inventory",
        "flags",
        "doors",
        "item_locations",
        "quests",
        "fired_events",
        "story_nodes",
        "turn",
        "version",
    ):
        old = getattr(before, field_name)
        new = getattr(after, field_name)
        if old != new:
            changed[field_name] = {
                "before": old.model_dump() if hasattr(old, "model_dump") else old,
                "after": new.model_dump() if hasattr(new, "model_dump") else new,
            }
    return changed
