"""A world whose inhabitants keep living after the player leaves.

Phase 3 of docs/hybrid-world-simulation.md, built out of the pieces Phase 1 and
2 already provide:

    perception (EventStore.witnessed_by)  ->  needs  ->  utility  ->  plan
        ->  ActionRequest(actor_type="npc")  ->  ActionService  ->  GameState

Nothing here writes state. An NPC's move is an ordinary `move` action proposed
with `actor_type="npc"`; the rules engine decides it, ActionService commits it,
EventStore records it, and the same CAS protects it as a player's turn. That is
the whole point: there is no NPC-only execution path to keep in sync.

Time is driven by the scheduler (Phase 2). Routines come from the world document
(`EntityDef.schedule`), motives are decided per decision point by
`npc_goals`, and the module only chooses *when* the next decision is.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.engines.event_engine import entity_location
from app.engines.game_clock import Clock
from app.engines.npc_goals import DEFAULT_NEEDS, Intent, choose_intent, score_intents
from app.engines import npc_schedule
from app.engines.npc_schedule import NPC_ROUTINE
from app.models.game import GameState, WorldDefinition

logger = logging.getLogger(__name__)

#: How often an NPC re-decides what to do (in-game minutes). Long enough that a
#: day is a decision or two, short enough to react to a theft within the hour.
DECISION_INTERVAL = 120

#: A need below this is worth acting on.
NEED_THRESHOLD = 0.35


@dataclass
class Decision:
    """One NPC's choice, with the numbers that produced it."""

    npc_id: str
    intent: str
    score: float
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    plan: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "npc_id": self.npc_id,
            "intent": self.intent,
            "score": self.score,
            "candidates": self.candidates,
            "plan": self.plan,
            "reason": self.reason,
        }


def _npc_entities(world: WorldDefinition) -> List[Any]:
    return [
        entity
        for entity in getattr(world, "entities", []) or []
        if getattr(entity, "kind", "npc") != "object"
    ]


def needs_of(record: Any) -> Dict[str, float]:
    """An NPC's needs, defaulted so an unauthored NPC is a neutral one."""
    stored = getattr(record, "needs", None) or {}
    out: Dict[str, float] = {}
    for name, default in DEFAULT_NEEDS.items():
        try:
            out[name] = max(0.0, min(1.0, float(stored.get(name, default))))
        except (TypeError, ValueError):
            out[name] = default
    return out


def perceive(
    store: Any,
    campaign_id: str,
    npc: Any,
    *,
    limit: int = 20,
) -> Dict[str, Any]:
    """What this NPC is entitled to know, taken from what it witnessed.

    `EventStore.witnessed_by` is the existing knowledge boundary: an NPC that
    did not see the theft cannot be told about it by consulting the world state.
    The returned dict is deliberately small and named, so a motive can only be
    built from facts the NPC actually has.
    """
    known: Dict[str, Any] = {"something_is_wrong": False, "ore_missing": False}
    if store is None or npc is None:
        return known
    try:
        rows = store.list_events(campaign_id, limit=200)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.debug("could not read events for %s: %s", getattr(npc, "id", "?"), exc)
        return known
    npc_id = npc_schedule.entity_id_of(npc)
    names = {npc_id, str(getattr(npc, "name", ""))} - {""}
    for row in rows[-limit:]:
        payload = getattr(row, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        witnesses = set(getattr(row, "witnessed_by", None) or [])
        if not (witnesses & names):
            # Not witnessed: the NPC simply does not have this fact, however
            # obvious it is in the authoritative state.
            continue
        action = str(payload.get("action") or "")
        code = str(payload.get("code") or "")
        if action in ("take", "steal") or code in ("item_taken", "theft"):
            known["something_is_wrong"] = True
            if action in ("take", "steal"):
                known["ore_missing"] = True
    return known


def decide(
    *,
    store: Any,
    campaign_id: str,
    state: GameState,
    world: WorldDefinition,
    npc: Any,
    current_activity: str = "",
    needs: Optional[Dict[str, float]] = None,
) -> Decision:
    """Pick one NPC's next intent and the primitives that carry it out.

    `needs` overrides the NPC's own record, which is how a caller asks "what
    would this one do if it were hungry" without writing state to find out.
    """
    record = state.entities.get(npc.id)
    location_id = (
        getattr(record, "location_id", "") or getattr(npc, "location_id", "") or ""
    )
    needs = dict(needs) if needs is not None else needs_of(record)
    known = perceive(store, campaign_id, npc)
    if npc.id in (state.combat.get("involved") or []):
        known["something_is_wrong"] = True
    work_location = ""
    for slot in getattr(npc, "schedule", None) or []:
        if str(getattr(slot, "activity", "")) == "work":
            work_location = str(getattr(slot, "location_id", "") or "")
            break
    profile = getattr(npc, "attributes", None) or {}
    scored = score_intents(
        npc,
        needs,
        known=known,
        has_work=bool(work_location),
        can_work_here=bool(work_location) and location_id == work_location,
        at_work=current_activity == "work",
        alarm=int(getattr(record, "alert", 0) or 0),
        current=current_activity,
    )
    best: Intent = choose_intent(scored)
    # "I am at work right now" is not a reason to keep standing there: an NPC
    # already at its workplace still has to be *doing* the work, which is a
    # different thing from being in the room. The driver applies the activity; a
    # shift that is only half over therefore keeps producing work, while a
    # finished shift falls back to the next best intent.
    if best.name == "work" and location_id == work_location:
        if shift_over(world, npc, location_id, current_activity, _now_abs(state)):
            best = _next_actionable(scored, exclude="work")
    from app.engines.npc_goals import plan_for  # local: avoids a cycle on import

    plan = plan_for(
        best.name,
        npc=npc,
        location_id=location_id,
        work_location_id=work_location,
    )
    return Decision(
        npc_id=npc.id,
        intent=best.name,
        score=best.score,
        candidates=[item.as_dict() for item in scored[:4]],
        plan=plan,
        reason=_reason(best, needs, known),
    )


def shift_over(
    world: WorldDefinition,
    npc: Any,
    location_id: str,
    current_activity: str,
    now_abs: int,
) -> bool:
    """Whether the NPC has already finished working wherever it stands.

    Standing in the workplace is not the same as working. Once the activity in
    force is the work slot and the NPC is already there, "go to work" has
    nothing left to do; the next best intent takes over instead. A shift with
    an authored length is still running until that length is up.
    """
    if current_activity != "work":
        return False
    found = npc_schedule.latest_slot_by_abs(world, npc_schedule.entity_id_of(npc), now_abs)
    if not found:
        return True
    started, payload = found
    if str(payload.get("activity", "")) != "work":
        return True
    here = str(payload.get("location_id", "") or "")
    if here and here != location_id:
        return False
    length = int(payload.get("minutes") or 0)
    if length <= 0:
        # A routine entry without a length is a single moment, not a shift.
        return True
    return now_abs >= int(started) + length


def _next_actionable(scored: List[Intent], *, exclude: str) -> Intent:
    """The best intent that still has something left to do."""
    for intent in scored:
        if intent.name != exclude:
            return intent
    return scored[0]


def _reason(intent: Intent, needs: Dict[str, float], known: Dict[str, Any]) -> str:
    """A sentence an author can read in the debug panel."""
    parts = [f"{name}={value:.2f}" for name, value in sorted(needs.items())]
    bits = [f"chose {intent.name} ({intent.score:.2f})", "needs " + ", ".join(parts)]
    if known.get("something_is_wrong"):
        bits.append("noticed something wrong")
    return "; ".join(bits)


def _now_abs(state: GameState) -> int:
    return Clock.from_state(state).abs_minute


def activity_at(
    world: WorldDefinition, npc: Any, abs_minute: int
) -> Tuple[str, str]:
    """The routine activity and location in force at a minute, if any."""
    found = npc_schedule.latest_slot_by_abs(world, npc_schedule.entity_id_of(npc), abs_minute)
    if not found:
        return "", ""
    payload = found[1]
    return str(payload.get("activity", "")), str(payload.get("location_id", ""))


def step_world(
    *,
    store: Any,
    campaign_id: str,
    state: GameState,
    world: WorldDefinition,
    limit: int = 8,
) -> Tuple[GameState, List[Dict[str, Any]]]:
    """One decision round for everyone in the world (no belief changes here).

    This is the *inspection* half of the loop: it produces the decisions and the
    domain-event rows that record them, and nothing else. Applying a decision is
    the caller's job, because applying one means submitting an ActionRequest -
    which must go through ActionService, not through this module.
    """
    now = _now_abs(state)
    rows: List[Dict[str, Any]] = []
    for npc in _npc_entities(world)[:limit]:
        activity, _location = activity_at(world, npc, now)
        decision = decide(
            store=store,
            campaign_id=campaign_id,
            state=state,
            world=world,
            npc=npc,
            current_activity=activity,
        )
        rows.append(
            {
                "event_type": "npc.decided",
                "payload": decision.as_dict(),
                "state_version": state.version,
            }
        )
    return state, rows


def next_decision_minute(state: GameState) -> int:
    return _now_abs(state) + DECISION_INTERVAL


__all__ = [
    "DECISION_INTERVAL",
    "NEED_THRESHOLD",
    "Decision",
    "activity_at",
    "decide",
    "needs_of",
    "next_decision_minute",
    "perceive",
    "step_world",
]
