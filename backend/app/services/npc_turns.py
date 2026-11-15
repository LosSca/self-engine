"""NPC behaviour is proposed, never written: it goes through ActionService.

Phase 3 of docs/hybrid-world-simulation.md. The pieces already exist - utility
scoring (`npc_goals`), perception (`npc_brain.perceive`), routines
(`npc_schedule`), the scheduler (`world_scheduler`) and the single writer
(`ActionService`). This module is the small amount of glue that turns a decision
into an `ActionRequest` with `actor_type="npc"`.

It is a *driver*, not an executor:

* it never mutates `GameState` - every change is committed by ActionService in
  the same transaction shape a player's turn uses;
* it never decides whether an action is allowed - the rules engine does, and a
  refusal is a normal answer here (the NPC simply did not manage it);
* it runs *after* the player's turn is durable, so a slow or failing NPC can
  never roll back or delay the player's committed turn.

The one thing that makes NPC turns different is `narrate=False`: an NPC's
morning does not deserve a paid model call, and the turn is recorded as a fact
either way.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.engines import npc_brain, npc_schedule
from app.engines.event_engine import entity_location
from app.engines.game_clock import Clock
from app.models.game import ActionRequest, GameState, WorldDefinition

logger = logging.getLogger(__name__)

#: How many NPC action attempts one round may make. A cap, not a budget: it
#: keeps a pathological world from turning one player turn into hundreds.
DEFAULT_ROUND_LIMIT = 6

#: The primitives an NPC is allowed to carry out on its own. Anything outside
#: this is refused here as well as by the rules engine - defence in depth for
#: "an NPC cannot do something a player could not".
NPC_VERBS = frozenset(
    {"move", "travel", "look", "talk", "investigate", "take", "open", "use", "wait"}
)


def _abs_minute(state: GameState) -> int:
    return Clock.from_state(state).abs_minute


def record_minds(event_store: Any, campaign_id: str, world: WorldDefinition, state: GameState) -> int:
    """Write down what each NPC is doing, as an `NPC_THOUGHT` event.

    The NPC panel reads that event type, and the legacy session was the only
    writer: a rules-lane campaign showed an empty panel with a working button.
    What is recorded is behaviour the utility function already decided - no
    second personality model, and no writer besides the event stream.
    """
    if event_store is None:
        return 0
    from app.db.event_store import EventType

    written = 0
    for entity in getattr(world, "entities", []) or []:
        if not getattr(entity, "schedule", None):
            continue
        record = state.entities.get(entity.id)
        here = entity_location(state, world, entity.id)
        activity = str(getattr(record, "activity", "") or "").strip()
        needs = dict(getattr(record, "needs", {}) or {})
        if not activity and not needs and not here:
            continue
        event_store.append(
            campaign_id,
            EventType.NPC_THOUGHT,
            {
                "name": entity.name,
                "npc_id": entity.id,
                "thoughts": {
                    "activity": activity or "idle",
                    "where": here,
                    "needs": {key: round(float(value), 3) for key, value in needs.items()},
                },
                "turn": int(getattr(state, "turn", 0) or 0),
            },
            narrative_time_delta=0,
            location=here,
            entities=[entity.id],
            # The panel shows a person's own state; it is not a rumour about
            # somebody else, so the NPC itself is the witness.
            witnessed_by=[entity.name],
        )
        written += 1
    return written


#: Marker for "the place, not a person". Handled by the caller, never sent to
#: the rules engine: the engine validates real targets.
_PLACE = "__here__"


def _audience(
    state: GameState, world: WorldDefinition, npc_id: str, *, intent: str = ""
) -> str:
    """Someone (or something) the NPC can actually address where it stands.

    Uses the *runtime* position of each entity, so an NPC that has walked away
    is no longer a possible audience. The player is never an audience: an NPC
    initiating a turn against the player's own persona is a conversation the
    engine cannot commit on the player's behalf.

    Falling back to the place is deliberate. An NPC that is alone is not
    inactive - "eat" and "socialise" still name real behaviour, and refusing
    them would make a town with one villager a town where nobody ever moves.
    """
    here = entity_location(state, world, npc_id) or state.location_id
    for entity in getattr(world, "entities", []) or []:
        if entity.id == npc_id:
            continue
        if entity_location(state, world, entity.id) != here:
            continue
        return entity.id
    return _PLACE if intent in ("eat", "socialise") else ""


def _npc_ids(world: WorldDefinition) -> List[str]:
    return [
        entity.id
        for entity in getattr(world, "entities", []) or []
        if getattr(entity, "kind", "npc") != "object"
    ]


def ensure_scheduled(
    store: Any,
    campaign_id: str,
    world: WorldDefinition,
    state: GameState,
    *,
    horizon: int = 1,
) -> int:
    """Queue the next days of routines. Returns how many slots were added.

    Idempotent: calling it every turn does not grow the table beyond one pending
    copy per (npc, slot).
    """
    if store is None:
        return 0
    day = max(1, int(state.day or 1))
    try:
        queued = npc_schedule.schedule_day(
            store, campaign_id, world, day=day, now_abs_minute=_abs_minute(state)
        )
        for offset in range(1, max(0, int(horizon)) + 1):
            queued.extend(
                npc_schedule.schedule_day(
                    store,
                    campaign_id,
                    world,
                    day=day + offset,
                    now_abs_minute=_abs_minute(state),
                )
            )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.warning("could not queue routines for %s: %s", campaign_id, exc)
        return 0
    return len(queued)


def start_world_clock(store: Any, campaign_id: str, *, horizon: int = 1) -> int:
    """Queue the days around the current moment of a campaign's own state.

    This is the cheap, store-only form of `ensure_scheduled`: a caller that owns
    a committed turn passes it straight to `ActionService.npc_clock`, and the
    world gains a calendar without anyone having to look up the world document.
    """
    if store is None:
        return 0
    state = store.get_state(campaign_id)
    if state is None:
        return 0
    world = store.get_world(state.world_id)
    if world is None:
        return 0
    return ensure_scheduled(store, campaign_id, world, state, horizon=horizon)


def routine_handler(
    service: Any,
) -> Any:
    """A scheduler handler that adds an NPC's routine to the world as activity.

    The handler does not move anyone: a routine is an *intention*, and moving by
    fiat would be a second write path with no rules behind it. It records what
    the NPC intends and lets the decision round (which runs through
    ActionService) actually do it.
    """

    def handler(store: Any, state: GameState, entry: Dict[str, Any]):
        payload = dict(entry.get("payload") or {})
        npc_id = str(payload.get("npc_id") or "")
        activity = str(payload.get("activity") or "idle")
        if not npc_id:
            return None
        record = state.entities.get(npc_id)
        if record is None:
            return None
        changed = 0
        if record.activity != activity:
            record.activity = activity
            changed += 1
        # Sleeping and eating are how a need is repaired; doing nothing is not.
        if activity == "sleep":
            previous = float(record.needs.get("energy", 0.8))
            record.needs["energy"] = min(1.0, previous + 0.25)
        elif activity == "eat":
            previous = float(record.needs.get("hunger", 0.8))
            record.needs["hunger"] = min(1.0, previous + 0.35)
        elif activity == "socialise":
            previous = float(record.needs.get("social", 0.6))
            record.needs["social"] = min(1.0, previous + 0.20)
        events = [
            {
                "event_type": "npc.routine",
                "payload": {
                    "npc_id": npc_id,
                    "activity": activity,
                    "location_id": str(payload.get("location_id") or ""),
                    "due_abs_minute": int(entry.get("due_abs_minute", 0)),
                },
            }
        ]
        return state, events, []

    return handler


async def run_round(
    service: Any,
    campaign_id: str,
    *,
    world: Optional[WorldDefinition] = None,
    limit: int = DEFAULT_ROUND_LIMIT,
    request_prefix: str = "npc",
    exclude: Sequence[str] = (),
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Let a few NPCs act, through the ordinary action pipeline.

    Returns `(results, failures)`. Both are informational: an NPC that could not
    do what it wanted is a normal outcome in a world with rules, and does not
    make the player's turn fail.

    Each NPC gets at most one attempt per round, and only when it wants
    something other than what it is already doing. `exclude` names NPCs that
    must not be decided for in this round - the player's own turn comes
    through this function too, and an actor must not act twice on one input.
    """
    store = getattr(service, "store", None)
    state = store.get_state(campaign_id) if store is not None else None
    if state is None:
        return [], ["no state"]
    if world is None:
        world = store.get_world(state.world_id)
    if world is None:
        return [], ["no world"]

    results: List[Dict[str, Any]] = []
    failures: List[str] = []
    now = _abs_minute(state)
    attempted = 0
    skip = {str(item) for item in exclude if item}
    for npc_id in _npc_ids(world):
        if npc_id in skip:
            continue
        if attempted >= max(0, int(limit)):
            break
        npc = next((e for e in world.entities if e.id == npc_id), None)
        if npc is None:
            continue
        # The NPC decides from the state *as it is now*, which the previous NPC
        # in this loop may already have changed.
        state = store.get_state(campaign_id)
        if state is None:
            break
        record = state.entities.get(npc_id)
        if record is not None and not record.alive:
            continue
        activity, _location = npc_brain.activity_at(world, npc, now)
        decision = npc_brain.decide(
            store=store,
            campaign_id=campaign_id,
            state=state,
            world=world,
            npc=npc,
            current_activity=activity or (record.activity if record else ""),
        )
        if not decision.plan:
            continue
        step = decision.plan[0]
        action = str(step.get("action") or "")
        if action not in NPC_VERBS:
            failures.append(f"{npc_id}: refused local verb '{action}'")
            continue
        target = step.get("target_id")
        if not target and action in ("move", "travel", "take", "open", "use"):
            continue
        params = dict(step.get("params") or {})
        if action == "travel":
            # The route validator needs to know whose position this is about.
            params.setdefault("actor_id", npc_id)
        elif action in ("talk", "investigate"):
            # An intent without an actor and a place is not an action. Talking to
            # nobody would be recorded as a real turn that changed nothing, so it
            # is dropped here instead - politely, and visibly in the failures.
            audience = _audience(state, world, npc_id, intent=decision.intent)
            if not audience:
                failures.append(f"{npc_id}: nothing here to {action} for {decision.intent}")
                continue
            if audience == _PLACE:
                # There is no person here, but the intent is still real: a
                # villager who wants company and finds nobody has to do the
                # thing itself. "wait" advances the clock, keeps the NPC in
                # place and costs the world nothing it has to roll back.
                action = "wait"
                target = None
                params.setdefault("minutes", 15)
            else:
                target = audience
        attempted += 1
        request = ActionRequest(
            campaign_id=campaign_id,
            request_id=f"{request_prefix}-{npc_id}-{uuid.uuid4().hex}",
            actor_type="npc",
            actor_id=npc_id,
            action=action,  # type: ignore[arg-type]
            target_id=target or None,
            params=params,
            narrate=False,
        )
        try:
            result = await service.execute(request)
        except LookupError as exc:
            failures.append(f"{npc_id}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one NPC must not stop the world
            logger.warning("npc %s round failed: %s", npc_id, exc, exc_info=True)
            failures.append(f"{npc_id}: {type(exc).__name__}: {exc}")
            continue
        results.append(
            {
                "npc_id": npc_id,
                "intent": decision.intent,
                "action": action,
                "target_id": target,
                "status": result.status,
                "code": result.code,
                "state_version": result.state_version,
            }
        )
    return results, failures


__all__ = [
    "DEFAULT_ROUND_LIMIT",
    "NPC_VERBS",
    "ensure_scheduled",
    "routine_handler",
    "run_round",
]
