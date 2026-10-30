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
    EntityDef,
    EntityState,
    EventExecution,
    GameEventDef,
    GameState,
    QuestProgress,
    WorldDefinition,
)


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


def entity_location(
    state: GameState, world: WorldDefinition, entity_id: str
) -> Optional[str]:
    """Where an entity is right now, or None when the world has no such entity.

    The authored EntityDef.location_id is the starting position; once anything
    moves the entity its EntityState record wins.
    """
    entity = next((e for e in world.entities if e.id == entity_id), None)
    if entity is None:
        return None
    record = state.entities.get(entity_id)
    if record is not None and record.location_id:
        return record.location_id
    return entity.location_id


def entity_state_for(
    state: GameState, world: WorldDefinition, entity: EntityDef
) -> EntityState:
    """The run-time record for an entity, seeded from its authored attributes."""
    existing = state.entities.get(entity.id)
    if existing is not None:
        return existing
    seed = entity.attributes if isinstance(entity.attributes, dict) else {}

    def _number(key: str, default: float) -> float:
        value = seed.get(key, default)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return default

    max_hp = max(1, int(_number("max_hp", _number("hp", 10))))
    record = EntityState(
        entity_id=entity.id,
        location_id=entity.location_id,
        hp=max_hp,
        max_hp=max_hp,
        alive=True,
    )
    state.entities[entity.id] = record
    return record


def _entity_alive(state: GameState, world: WorldDefinition, entity_id: str) -> bool:
    combatant = state.combatants.get(entity_id)
    if combatant is not None and not combatant.alive:
        return False
    record = state.entities.get(entity_id)
    if record is not None:
        return record.alive
    return True


def condition_holds(
    state: GameState, world: WorldDefinition, condition: Condition
) -> bool:
    kind = condition.kind
    if kind in ("entity_at", "entity_alive", "combatant_hp", "combatant_alive"):
        if not any(entity.id == condition.target for entity in world.entities):
            return False
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
        return _compare(_door_is_open(state, world, condition.target), expected, condition.op)
    if kind == "at_location":
        expected = True if condition.value is None else condition.value
        return _compare(state.location_id == condition.target, expected, condition.op)
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
    if kind == "entity_at":
        here = entity_location(state, world, condition.target)
        return (
            here is not None
            and isinstance(condition.value, str)
            and _compare(here, condition.value, condition.op)
        )
    if kind == "entity_alive":
        expected = True if condition.value is None else bool(condition.value)
        return _compare(
            _entity_alive(state, world, condition.target), expected, condition.op
        )
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
        if not quest.enabled:
            return AppliedEffect(effect, False, f"quest '{quest.id}' is disabled")
        progress = state.quests.get(quest.id)
        if progress is None or progress.status != "active":
            return AppliedEffect(effect, False, f"quest '{quest.id}' is not active")
        current = progress.objectives.get(objective.id, 0)
        updated = min(objective.required_count, current + max(1, int(effect.amount or 1)))
        if updated == current:
            return AppliedEffect(effect, True)
        progress.objectives[objective.id] = updated
        state.quests[quest.id] = progress
        return AppliedEffect(
            effect,
            True,
            facts=[Fact("quest_progress", f"{quest.id}:{objective.id}")],
        )

    if kind == "quest_complete":
        from app.engines.quest_engine import complete_quest

        return complete_quest(state, world, effect)

    if kind == "quest_accept":
        from app.engines.quest_engine import accept_conditions_met, prerequisites_met

        quest = next((q for q in world.quests if q.id == effect.target), None)
        if quest is None:
            return AppliedEffect(effect, False, f"unknown quest '{effect.target}'")
        if not quest.enabled:
            return AppliedEffect(effect, False, f"quest '{quest.id}' is disabled")
        progress = state.quests.get(quest.id) or QuestProgress(
            quest_id=quest.id, status="inactive"
        )
        if progress.status in ("completed", "failed"):
            return AppliedEffect(
                effect, False, f"quest '{quest.id}' is already {progress.status}"
            )
        if not prerequisites_met(state, quest):
            return AppliedEffect(effect, False, f"quest '{quest.id}' prerequisites are unmet")
        if not accept_conditions_met(state, world, quest):
            return AppliedEffect(effect, False, f"quest '{quest.id}' accept conditions are unmet")
        if progress.status == "active":
            return AppliedEffect(effect, True)
        for objective in quest.objectives:
            progress.objectives.setdefault(objective.id, 0)
        progress.status = "active"
        state.quests[quest.id] = progress
        return AppliedEffect(effect, True, facts=[Fact("quest_accepted", quest.id)])

    if kind == "quest_fail":
        quest = next((q for q in world.quests if q.id == effect.target), None)
        if quest is None:
            return AppliedEffect(effect, False, f"unknown quest '{effect.target}'")
        if not quest.enabled:
            return AppliedEffect(effect, False, f"quest '{quest.id}' is disabled")
        progress = state.quests.get(quest.id) or QuestProgress(quest_id=quest.id)
        if progress.status == "completed":
            return AppliedEffect(
                effect, False, f"quest '{quest.id}' is already completed"
            )
        if progress.status == "failed":
            return AppliedEffect(effect, True)
        progress.status = "failed"
        state.quests[quest.id] = progress
        return AppliedEffect(effect, True, facts=[Fact("quest_failed", quest.id)])

    if kind == "combat_hp":
        entity = next((entry for entry in world.entities if entry.id == effect.target), None)
        if entity is None:
            return AppliedEffect(effect, False, f"unknown entity '{effect.target}'")
        try:
            hp = 0 if effect.value is None else int(effect.value)
        except (TypeError, ValueError, OverflowError):
            return AppliedEffect(
                effect, False, "combat_hp requires a numeric value convertible to an integer"
            )
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
        combatant.hp = max(0, min(hp, combatant.max_hp))
        was_alive = combatant.alive
        combatant.alive = combatant.hp > 0
        state.combatants[entity.id] = combatant
        record = entity_state_for(state, world, entity)
        record.hp = combatant.hp
        record.max_hp = combatant.max_hp
        record.alive = combatant.alive
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

    if kind == "move_entity":
        entity = next((e for e in world.entities if e.id == effect.target), None)
        if entity is None:
            return AppliedEffect(effect, False, f"unknown entity '{effect.target}'")
        if not isinstance(effect.value, str):
            return AppliedEffect(effect, False, "move_entity requires value = location id (string)")
        destination = effect.value
        location = next((l for l in world.locations if l.id == destination), None)
        if location is None:
            return AppliedEffect(effect, False, f"unknown location '{destination}'")
        record = entity_state_for(state, world, entity)
        origin = record.location_id or entity.location_id
        record.location_id = location.id
        return AppliedEffect(
            effect,
            True,
            facts=[Fact("entity_moved", entity.id, location.id)],
        )

    if kind == "seed_entity":
        entity = next((e for e in world.entities if e.id == effect.target), None)
        if entity is None:
            return AppliedEffect(effect, False, f"unknown entity '{effect.target}'")
        if entity.id in state.entities:
            # Already materialised: seeding is idempotent by construction.
            return AppliedEffect(effect, True)
        entity_state_for(state, world, entity)
        return AppliedEffect(
            effect, True, facts=[Fact("entity_seeded", entity.id, entity.location_id)]
        )

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


def _repeat_reason(event: GameEventDef, state: GameState) -> str:
    """Why this event may not fire again right now (empty means it may)."""
    if event.repeat == "once" and event.id in state.fired_events:
        return "already fired (repeat=once)"
    count = state.event_counts.get(event.id, 0)
    if event.max_fires is not None and count >= event.max_fires:
        return f"max_fires reached ({count}/{event.max_fires})"
    if event.cooldown_turns:
        last = state.event_last_turn.get(event.id)
        if last is not None and state.turn - last < event.cooldown_turns:
            remaining = event.cooldown_turns - (state.turn - last)
            return f"cooldown: {remaining} more turn(s)"
    return ""


def run_triggered_events(
    world: WorldDefinition,
    state: GameState,
    action: str,
    target_id: Optional[str],
    facts: List[Fact],
    *,
    processed: Optional[set[str]] = None,
    forced: Optional[set[str]] = None,
) -> Tuple[GameState, List[EventExecution], List[Fact]]:
    """Settle matching events in priority order, attempting each once per action.

    Each event is applied atomically: effects go onto a scratch copy of the
    state, and a failing effect discards that copy and records a failure with a
    readable reason. Follow-up event ids (success or failure) join `forced`, so
    they may run in this same settlement even when their own trigger does not
    match; their conditions still decide. Returns (new_state, executions, facts).
    """
    state = state.model_copy(deep=True)
    processed = processed if processed is not None else set()
    forced = forced if forced is not None else set()
    executions: List[EventExecution] = []
    new_facts: List[Fact] = []
    ordered = sorted(world.events, key=lambda e: (-e.priority, e.id))
    known = {event.id for event in ordered}

    for _ in range(len(ordered)):
        fired = False
        for event in ordered:
            if event.id in processed:
                continue
            if event.id not in forced and not event_matches(event, action, target_id):
                continue
            if not event.enabled:
                continue
            if event.repeat == "once" and event.id in state.fired_events:
                processed.add(event.id)
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
            blocked = _repeat_reason(event, state)
            if blocked:
                processed.add(event.id)
                executions.append(
                    EventExecution(
                        event_id=event.id,
                        name=event.name,
                        status="skipped",
                        reason=blocked,
                    )
                )
                continue
            processed.add(event.id)
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
                forced.update(fid for fid in event.failure_events if fid in known)
                continue
            state = scratch
            fired = True
            if event.id not in state.fired_events:
                state.fired_events.append(event.id)
            state.event_counts[event.id] = state.event_counts.get(event.id, 0) + 1
            state.event_last_turn[event.id] = state.turn
            for item in applied:
                new_facts.extend(item.facts)
            forced.update(fid for fid in event.followup_events if fid in known)
            executions.append(
                EventExecution(
                    event_id=event.id,
                    name=event.name,
                    status="fired",
                    effects=event.effects,
                )
            )
        if not fired:
            break
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
                effect, item_ids, door_ids, quest_ids, entity_ids, location_ids
            )
        )
    if event.trigger == "action" and not event.trigger_action:
        errors.append("trigger 'action' requires trigger_action")
    known_events = {candidate.id for candidate in world.events}
    for followup_id in event.followup_events + event.failure_events:
        if followup_id == event.id:
            errors.append("an event cannot name itself as a follow-up")
        elif followup_id not in known_events:
            errors.append(f"unknown follow-up event '{followup_id}'")
    return errors


def simulate_event(
    world: WorldDefinition, state: GameState, event_id: str
) -> Dict[str, Any]:
    """Preview one event without touching the stored state."""
    event = next((e for e in world.events if e.id == event_id), None)
    if event is None:
        return {"status": "not_found", "error": f"unknown event '{event_id}'"}
    errors = validate_event(world, event)
    conditions_met = False
    reason = ""
    scratch = state
    applied: List[AppliedEffect] = []
    failed: List[AppliedEffect] = []
    if errors:
        status = "invalid"
    elif not event.enabled:
        status = "disabled"
    else:
        blocked = _repeat_reason(event, state)
        if blocked:
            status = "skipped"
            reason = blocked
        else:
            conditions_met = all(condition_holds(state, world, c) for c in event.conditions)
            if not conditions_met:
                status = "not_applicable"
            else:
                scratch = state.model_copy(deep=True)
                applied = apply_effects(scratch, world, event.effects)
                failed = [item for item in applied if not item.ok]
                status = "failed" if failed else "would_fire"
                if failed:
                    scratch = state
                else:
                    if event.id not in scratch.fired_events:
                        scratch.fired_events.append(event.id)
                    scratch.event_counts[event.id] = scratch.event_counts.get(event.id, 0) + 1
                    scratch.event_last_turn[event.id] = scratch.event_last_turn.get(
                        event.id, scratch.turn
                    )
    return {
        "status": status,
        "reason": reason,
        "event_id": event.id,
        "name": event.name,
        "errors": errors,
        "conditions_met": conditions_met,
        "followup_events": list(event.followup_events),
        "failure_events": list(event.failure_events),
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
        "event_counts",
        "event_last_turn",
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
