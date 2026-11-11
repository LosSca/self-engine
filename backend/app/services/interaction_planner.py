"""Plan what an author can do *here*, before anything is committed (requirement 5).

An author editing a world wants to answer one question: "if the player stands in
this room right now, what can they actually do, and why not the rest?".  The
rules engine already answers that for one action at a time; this module asks it
for a whole room, then answers the harder follow-up questions with the same
deterministic machinery:

* ``plan_room`` - every candidate action in the current room, allowed or not.
* ``run_preview`` - replay a chain of actions on a copy and report what each
  step would change and which events it would settle.
* ``explain_requirement`` - how far away a blocked action is ("the door needs
  the iron key, and that key lies in the yard two rooms away").

Everything here is read-only: the state is deep-copied before any step runs and
nothing is written back, so a preview can never change a save.  The verdicts are
not invented here; they come from ``RulesEngine.evaluate`` and the same event
engine a real turn uses.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.engines import event_engine
from app.engines.event_engine import AppliedEffect, Fact
from app.engines.rules_engine import RulesEngine
from app.models.game import GameState, RuleOutcome, WorldDefinition

#: Effects that only announce something.  A step that produced nothing but
#: these did not change the world, so it cannot unblock a follow-up action.
_INERT_EFFECTS = ("emit",)

MAX_PREVIEW_STEPS = 6

#: How many candidate actions one room plan enumerates.
MAX_CANDIDATES = 40

#: Upper bound on the breadth-first walk used by explain_requirement.
MAX_PATH_DEPTH = 6

_PREREQ_NOTE = "prerequisites unmet"


class PlanError(ValueError):
    """The plan itself was impossible (no steps, bad target, too long)."""


def _rules(engine: Optional[RulesEngine] = None) -> RulesEngine:
    return engine or RulesEngine()


def _describe(outcome: RuleOutcome) -> Dict[str, Any]:
    return {
        "ok": outcome.ok,
        "code": outcome.code,
        "message": outcome.message,
        "effects": [effect.model_dump() for effect in outcome.effects],
    }


def _snapshot(state: GameState) -> Dict[str, Any]:
    """The parts of the state a previewed action can move."""
    return {
        "location_id": state.location_id,
        "inventory": dict(state.inventory),
        "flags": dict(state.flags),
        "doors": dict(state.doors),
        "item_locations": dict(state.item_locations),
        "quests": {key: value.status for key, value in state.quests.items()},
    }


_DICT_FIELDS = ("inventory", "flags", "doors", "item_locations", "quests")


def _changes(before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Field-level diff between two snapshots (empty means nothing moved)."""
    rows: List[Dict[str, Any]] = []
    if before.get("location_id") != after.get("location_id"):
        rows.append({
            "field": "location_id",
            "target": "",
            "before": before.get("location_id"),
            "after": after.get("location_id"),
        })
    for field in _DICT_FIELDS:
        old = before.get(field) or {}
        new = after.get(field) or {}
        for name in sorted(set(old) | set(new)):
            if old.get(name) != new.get(name):
                rows.append({
                    "field": field,
                    "target": name,
                    "before": old.get(name),
                    "after": new.get(name),
                })
    return rows


def _apply(
    world: WorldDefinition, state: GameState, effects: Sequence[Any]
) -> Tuple[GameState, List[AppliedEffect], List[str]]:
    """Apply effects to a copy, reporting the ones that failed."""
    draft = state.model_copy(deep=True)
    applied = event_engine.apply_effects(draft, world, list(effects))
    errors = [item.error or "effect failed" for item in applied if not item.ok]
    return draft, applied, errors


def _facts(applied: Sequence[AppliedEffect]) -> List[Fact]:
    facts: List[Fact] = []
    for item in applied:
        if item.ok:
            facts.extend(item.facts)
    return facts


def room_candidates(
    world: WorldDefinition, state: GameState, limit: int = MAX_CANDIDATES
) -> List[Dict[str, Any]]:
    """Every (action, target) worth asking the rules engine about, here and now."""
    seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    location = next(
        (item for item in world.locations if item.id == state.location_id), None
    )
    if location is None:
        return []

    def add(action: str, target: str, label: str) -> None:
        key = (action, target)
        if key in seen or len(seen) >= limit:
            return
        seen[key] = {"action": action, "target": target, "label": label}

    for exit_def in location.exits:
        add("move", exit_def.target_id, "go " + (exit_def.label or exit_def.target_id))
    for item in world.items:
        if state.inventory.get(item.id, 0) > 0:
            add("use", item.id, "use " + item.name)
        else:
            add("take", item.id, "pick up " + item.name)
    for door in world.doors:
        if door.location_id == state.location_id:
            add("open", door.id, "open " + door.name)
    for entity in world.entities:
        here = event_engine.entity_location(state, world, entity.id)
        if here != state.location_id:
            continue
        add("talk", entity.id, "talk to " + entity.name)
        add("attack", entity.id, "attack " + entity.name)
    for action in world.custom_actions:
        add("custom", action.id, action.label or action.id)
    return list(seen.values())


def plan_room(
    world: WorldDefinition,
    state: GameState,
    engine: Optional[RulesEngine] = None,
    limit: int = MAX_CANDIDATES,
) -> Dict[str, Any]:
    """What is possible here and now, decided by the rules engine itself."""
    rules = _rules(engine)
    allowed: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    for candidate in room_candidates(world, state, limit):
        params: Dict[str, Any] = {}
        if candidate["action"] == "move":
            params["path"] = [candidate["target"]]
        outcome = rules.evaluate(
            world, state, candidate["action"], candidate["target"], params
        )
        row = {
            "action": candidate["action"],
            "target": candidate["target"],
            "label": candidate["label"],
            "ok": outcome.ok,
            "code": outcome.code,
            "message": outcome.message,
        }
        (allowed if outcome.ok else blocked).append(row)
    return {
        "campaign_id": state.campaign_id,
        "world_id": world.world_id,
        "location_id": state.location_id,
        "version": state.version,
        "allowed": allowed,
        "blocked": blocked,
    }


def _followups(
    world: WorldDefinition,
    state: GameState,
    action: str,
    target_id: str,
    facts: Sequence[Fact],
) -> Tuple[List[Dict[str, Any]], List[Fact], List[str]]:
    """The events a step would settle, decided by the same engine as a turn.

    Runs on the (already copied) state, so fired flags, cooldowns and conditions
    are honoured exactly as they would be for a committed action.  Returns the
    execution rows, the facts those events produced and the effect errors.
    """
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    produced: List[Fact] = []
    queue: List[str] = [
        event.id
        for event in world.events
        if event_engine.event_matches(event, action, target_id)
    ]
    for fact in facts:
        if fact.kind == "emit" and fact.target:
            queue.append(str(fact.target))

    while queue:
        event_id = queue.pop(0)
        event = next((item for item in world.events if item.id == event_id), None)
        if event is None:
            events.append(
                {"event_id": event_id, "status": "skipped", "reason": "unknown event"}
            )
            continue
        if event.id in state.fired_events:
            continue
        reason = event_engine._repeat_reason(event, state)
        if reason:
            events.append({"event_id": event.id, "status": "skipped", "reason": reason})
            continue
        if not all(
            event_engine.condition_holds(state, world, condition)
            for condition in event.conditions
        ):
            continue
        effects = list(event.effects)
        branch = event_engine._matching_branch(event, state, world)
        if branch is not None:
            effects.extend(branch.effects)
        applied = event_engine.apply_effects(state, world, effects)
        failed = [item for item in applied if not item.ok]
        if failed:
            message = failed[0].error or "effect failed"
            errors.extend(message for _ in failed)
            events.append(
                {"event_id": event.id, "status": "failed", "reason": message}
            )
            continue
        state.fired_events.append(event.id)
        events.append({"event_id": event.id, "status": "fired", "reason": ""})
        step_facts = _facts(applied)
        produced.extend(step_facts)
        for fact in step_facts:
            if fact.kind == "emit" and fact.target and str(fact.target) not in queue:
                queue.append(str(fact.target))
        for follow_id in event.followup_events:
            if follow_id not in queue:
                queue.append(follow_id)
    return events, produced, errors


def _run_step(
    world: WorldDefinition,
    state: GameState,
    rules: RulesEngine,
    action: str,
    target: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Evaluate one action on ``state`` and, if allowed, apply it in place.

    ``state`` is mutated by the caller-visible success path; pass a copy when
    the caller must keep the original (which every public entry point does).
    """
    params = dict(params or {})
    if action == "move" and not params.get("path"):
        params["path"] = [target]
    before = _snapshot(state)
    outcome = rules.evaluate(world, state, action, target or None, params)
    row: Dict[str, Any] = {
        "action": action,
        "target": target,
        "params": params,
        **_describe(outcome),
        "events": [],
        "changes": [],
        "effect_errors": [],
    }
    if not outcome.ok:
        row["advanced"] = False
        return {"row": row, "state": state, "outcome": outcome, "applied": False}

    draft, applied, errors = _apply(world, state, outcome.effects)
    events, _, event_errors = _followups(
        world, draft, action, target, _facts(applied)
    )
    row["events"] = events
    row["changes"] = _changes(before, _snapshot(draft))
    row["effect_errors"] = errors + event_errors
    row["advanced"] = bool(row["changes"])
    return {"row": row, "state": draft, "outcome": outcome, "applied": True}


def run_preview(
    world: WorldDefinition,
    state: GameState,
    steps: Sequence[Dict[str, Any]],
    engine: Optional[RulesEngine] = None,
) -> Dict[str, Any]:
    """Replay authored actions on a copy: what would each step change?

    Returns ``simulated: True`` and the unchanged ``start_version``: the console
    can show the chain without any risk of it counting as a turn.  The first
    rejected step ends the replay, because later steps were written against the
    state that rejection just invalidated.
    """
    rules = _rules(engine)
    plan = list(steps)
    if not plan:
        raise PlanError("preview needs at least one step")
    if len(plan) > MAX_PREVIEW_STEPS:
        raise PlanError("a preview is limited to %d steps" % MAX_PREVIEW_STEPS)

    draft = state.model_copy(deep=True)
    rows: List[Dict[str, Any]] = []
    for index, step in enumerate(plan):
        if not isinstance(step, dict):
            raise PlanError("step %d is not an object" % (index + 1))
        action = str(step.get("action") or "").strip()
        target = str(step.get("target") or "").strip()
        if not action:
            raise PlanError("step %d has no action" % (index + 1))
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        result = _run_step(world, draft, rules, action, target, params)
        row = result["row"]
        row["step"] = index + 1
        rows.append(row)
        if not result["applied"]:
            break
        draft = result["state"]

    stopped = bool(rows) and not rows[-1]["ok"]
    return {
        "campaign_id": state.campaign_id,
        "world_id": world.world_id,
        "start_version": state.version,
        "steps": rows,
        "stopped_at": rows[-1]["step"] if stopped else 0,
        "simulated": True,
        "step_limit": MAX_PREVIEW_STEPS,
    }


def _reachable(world: WorldDefinition, state: GameState) -> Dict[str, int]:
    """Shortest number of moves from here to every reachable location.

    Movement is asked from the rules engine rather than re-derived here, so a
    corridor behind a locked door correctly stays out of reach.
    """
    rules = RulesEngine()
    distances: Dict[str, int] = {state.location_id: 0}
    queue: deque = deque([(state.location_id, state, 0)])
    while queue:
        current_id, current_state, depth = queue.popleft()
        if depth >= MAX_PATH_DEPTH:
            continue
        location = next(
            (item for item in world.locations if item.id == current_id), None
        )
        if location is None:
            continue
        for exit_def in location.exits:
            target = exit_def.target_id
            if target in distances:
                continue
            outcome = rules.evaluate(
                world, current_state, "move", target, {"path": [target]}
            )
            if not outcome.ok:
                continue
            distances[target] = depth + 1
            moved, _, _ = _apply(world, current_state, outcome.effects)
            queue.append((target, moved, depth + 1))
    return distances


def _floor_placement(world: WorldDefinition, state: GameState) -> Dict[str, Dict[str, str]]:
    """Where each item is right now: carried, held by an entity, or on the floor."""
    placement: Dict[str, Dict[str, str]] = {}
    for item in world.items:
        if state.inventory.get(item.id, 0) > 0:
            placement[item.id] = {"kind": "carried", "location_id": ""}
            continue
        holder = ""
        for entity in world.entities:
            here = event_engine.entity_location(state, world, entity.id)
            if here is None or here != state.location_id:
                continue
            if event_engine.entity_item_count(state, world, entity.id, item.id) > 0:
                holder = entity.id
                break
        if holder:
            placement[item.id] = {
                "kind": "held",
                "location_id": state.location_id,
                "entity_id": holder,
            }
            continue
        where = state.item_locations.get(item.id, "")
        placement[item.id] = {
            "kind": "floor" if where else "unknown",
            "location_id": where,
        }
    return placement


def _first_blocking_door(
    world: WorldDefinition, state: GameState
) -> Tuple[str, str]:
    """The first exit that is closed by a door, and that door id.

    Used when a requirement sits in a room the player cannot walk to: the door
    is the real obstacle, and the key to *it* is the next requirement.
    """
    rules = RulesEngine()
    location = next(
        (item for item in world.locations if item.id == state.location_id), None
    )
    if location is None:
        return "", ""
    for exit_def in location.exits:
        outcome = rules.evaluate(
            world, state, "move", exit_def.target_id, {"path": [exit_def.target_id]}
        )
        if outcome.ok:
            continue
        if outcome.code in ("missing_item", "exit_blocked") and exit_def.door_id:
            return exit_def.target_id, exit_def.door_id
    return "", ""


def _requirement_for_door(world: WorldDefinition, door_id: str) -> Optional[Dict[str, Any]]:
    door = next((item for item in world.doors if item.id == door_id), None)
    if door is None or not door.locked or not door.key_item_id:
        return None
    return {"kind": "has_item", "target": door.key_item_id, "label": door.name}


def _plan_condition(
    world: WorldDefinition, state: GameState, kind: str, target: str
) -> Optional[Dict[str, Any]]:
    """How to satisfy one unmet requirement, when playing can satisfy it.

    Only the shapes an author can fix by walking and picking up are planned;
    anything else lands in ``unplannable`` instead of being guessed at.
    """
    distances = _reachable(world, state)
    if kind == "has_item":
        item = next((entry for entry in world.items if entry.id == target), None)
        if item is None:
            return None
        placement = _floor_placement(world, state).get(target, {})
        where = str(placement.get("location_id") or "")
        steps: List[str] = []
        blocked_door = ""
        if where and where != state.location_id:
            if where in distances:
                steps.append("move " + where)
            else:
                _, blocked_door = _first_blocking_door(world, state)
        steps.append("take " + target)
        return {
            "kind": "obtain_item",
            "target": target,
            "label": item.name,
            "item_location": where,
            "item_holder": placement.get("entity_id", ""),
            "distance": distances.get(where, -1),
            "steps": steps,
            "blocked_by_door": blocked_door,
        }
    if kind == "at_location":
        if target == state.location_id:
            return None
        reachable = target in distances
        blocked_door = ""
        if not reachable:
            _, blocked_door = _first_blocking_door(world, state)
        return {
            "kind": "reach_location",
            "target": target,
            "label": target,
            "distance": distances.get(target, -1),
            "steps": ["move " + target] if reachable else [],
            "blocked_by_door": blocked_door,
        }
    return None


def _requirements_of(
    world: WorldDefinition, action: str, target_id: str
) -> List[Tuple[str, str]]:
    """The (kind, target) pairs an action needs, read from the world document.

    These are the authored requirements, not a second rule implementation: for a
    locked door the rules engine reports ``door_locked`` and the door document
    names its key; for a guarded item the item document names its conditions.
    """
    pairs: List[Tuple[str, str]] = []
    if action == "open":
        door = next((item for item in world.doors if item.id == target_id), None)
        if door is not None and door.locked and door.key_item_id:
            pairs.append(("has_item", door.key_item_id))
    if action == "take":
        item = next((entry for entry in world.items if entry.id == target_id), None)
        if item is not None:
            for condition in item.pickup_requires:
                if condition.kind in ("has_item", "at_location"):
                    pairs.append((condition.kind, str(condition.target or "")))
    if action == "move":
        for location in world.locations:
            for exit_def in location.exits:
                if exit_def.target_id != target_id:
                    continue
                if exit_def.requires_item_id:
                    pairs.append(("has_item", exit_def.requires_item_id))
                if exit_def.door_id:
                    door = next(
                        (item for item in world.doors if item.id == exit_def.door_id),
                        None,
                    )
                    if door is not None and door.locked and door.key_item_id:
                        pairs.append(("has_item", door.key_item_id))
    return pairs


def _plain_sentence(action: str, target_id: str, outcome: Any) -> str:
    """The readable answer shown next to a rejected action."""
    if outcome.message:
        return outcome.message
    return "the rules engine rejected %s %s" % (action, target_id)


def explain_requirement(
    world: WorldDefinition,
    state: GameState,
    action: str,
    target_id: str,
    engine: Optional[RulesEngine] = None,
) -> Dict[str, Any]:
    """Why an action is blocked, and the shortest way to unblock it.

    Read-only.  When the action is already allowed the answer says so and lists
    no requirements, which is what the console shows next to a green tick.
    """
    rules = _rules(engine)
    params: Dict[str, Any] = {"path": [target_id]} if action == "move" else {}
    outcome = rules.evaluate(world, state, action, target_id or None, params)
    body: Dict[str, Any] = {
        "campaign_id": state.campaign_id,
        "action": action,
        "target": target_id,
        "ok": outcome.ok,
        "code": outcome.code,
        "message": _plain_sentence(action, target_id, outcome),
        "requirements": [],
        "unplannable": [],
    }
    if outcome.ok:
        return body

    plans: List[Dict[str, Any]] = []
    unplannable: List[str] = []
    budget = 4
    for kind, target in _requirements_of(world, action, target_id):
        if budget <= 0:
            break
        plan = _plan_condition(world, state, kind, target)
        if plan is None:
            if (kind, target) not in [tuple(item) for item in unplannable]:
                unplannable.append([kind, target])
            continue
        # A requirement hidden behind a locked door is only reachable once the
        # key of that door is in hand, so name that key as a second requirement.
        door_id = plan.get("blocked_by_door") or ""
        if door_id:
            key_plan = _requirement_for_door(world, door_id)
            if key_plan is not None and key_plan["target"] != target:
                chained = _plan_condition(world, state, "has_item", key_plan["target"])
                if chained is not None:
                    chained["label"] = key_plan["label"]
                    chained["unlocks_door"] = door_id
                    plan.setdefault("prerequisite", chained)
        plans.append(plan)
        budget -= 1

    body["requirements"] = plans
    body["unplannable"] = unplannable
    body["has_plan"] = bool(plans) and not unplannable
    return body


_QUEST_OBTAIN = ("obtain_item", "has_item")
_QUEST_OPEN = ("open_door",)
_QUEST_REACH = ("reach_location", "at_location")
_QUEST_FLAG = ("flag",)


def _objective_effects(
    world: WorldDefinition, objective: Any
) -> Tuple[List[Any], List[str]]:
    """What a previewed step must produce for one objective to count.

    ``Goal`` is a simulation model: "for this quest objective to count, these
    facts must appear".  Each objective kind maps to one effect kind, so the
    preview reuses the real effect handlers rather than a parallel rule set.
    """
    from app.models.game import Effect

    notes: List[str] = []
    if objective.kind in _QUEST_OBTAIN:
        return [Effect(kind="give_item", target=objective.target, amount=1)], notes
    if objective.kind in _QUEST_OPEN:
        return [Effect(kind="open_door", target=objective.target)], notes
    if objective.kind in _QUEST_REACH:
        return [Effect(kind="move_player", target=objective.target)], notes
    if objective.kind in _QUEST_FLAG:
        return [Effect(kind="set_flag", target=objective.target, value=True)], notes
    notes.append("objective kind not previewable: " + str(objective.kind))
    return [], notes


def quests_this_step(
    world: WorldDefinition, state: GameState, facts: Sequence[Fact]
) -> List[Dict[str, Any]]:
    """Quest objectives a set of facts would advance (read-only projection).

    Quests themselves are settled only by a committed action; this is the
    "what would this unlock" line the console shows, computed the same way the
    quest engine would see it.
    """
    rows: List[Dict[str, Any]] = []
    seen = {(fact.kind, str(fact.target)) for fact in facts}
    for quest in world.quests:
        for objective in quest.objectives:
            wanted, notes = _objective_effects(world, objective)
            hit = False
            for effect in wanted:
                if (effect.kind, str(effect.target)) in seen:
                    hit = True
                elif effect.kind == "give_item" and ("item_gained", str(effect.target)) in seen:
                    hit = True
                elif effect.kind == "open_door" and ("door_opened", str(effect.target)) in seen:
                    hit = True
                elif effect.kind == "move_player" and ("reach_location", str(effect.target)) in seen:
                    hit = True
                elif effect.kind == "set_flag" and ("flag", str(effect.target)) in seen:
                    hit = True
            if hit:
                rows.append({
                    "quest_id": quest.id,
                    "objective_id": objective.id,
                    "notes": notes,
                })
    return rows


def sample_state(world: WorldDefinition, campaign_id: str = "preview") -> GameState:
    """A throwaway starting state, for planning a world nobody is playing."""
    from app.services.world_service import build_initial_state

    state = build_initial_state(world, campaign_id)
    return state
