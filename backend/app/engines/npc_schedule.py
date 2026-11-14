"""Turn authored routines into schedules the scheduler can carry out.

Phase 3 of docs/hybrid-world-simulation.md. An NPC's day is written in the world
document ("the smith works in the forge from 09:00 for eight hours"), but the
scheduler only knows absolute minutes. This module is the one place that turns
one into the other:

    EntryDef.schedule  ->  schedule entries  ->  `scheduled_events` rows

It is deliberately a *pure* compiler. It holds no state, writes nothing and
decides nothing about what an NPC may do: a routine becomes a handler call, and
the handler still has to get its action past the rules engine.

Only what is inside the day is compiled. Interpersonal motives ("go to the
market because the ore is gone") are not routines and must never be scheduled -
they are decided at each decision point by the NPC brain, which can react to
what happened five minutes ago, which a static schedule cannot.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.engines.game_clock import midnight
from app.engines.world_scheduler import plan_tick
from app.models.game import GameState, WorldDefinition

#: The scheduler `kind` of a routine slot. One kind, one handler: the slot's own
#: `activity` distinguishes working from sleeping inside the payload.
NPC_ROUTINE = "npc.routine"

#: Minutes in a day; duplicated rather than imported so this module stays free
#: of the clock's state ownership.
MINUTES_PER_DAY = 1440


def entity_id_of(npc: Any) -> str:
    """The entity id of an NPC, accepted as either an EntityDef or a bare id."""
    return npc if isinstance(npc, str) else str(getattr(npc, "id", ""))


def _minutes_of_day(raw: str) -> Optional[int]:
    """Parse "HH:MM" / "H:MM" into minutes since midnight."""
    text = str(raw or "").strip()
    if not text:
        return None
    head, _, tail = text.partition(":")
    try:
        hour = int(head)
        minute = int(tail or 0)
    except (TypeError, ValueError):
        return None
    if hour < 0 or hour > 24 or minute < 0 or minute > 59:
        return None
    return min(MINUTES_PER_DAY - 1, hour * 60 + minute)


def routine_entries(
    world: WorldDefinition, day: int
) -> List[Dict[str, Any]]:
    """Every routine slot of every NPC on one day, as scheduler entries.

    One entry per (npc, slot). The entry's `due_abs_minute` is absolute - so the
    same world on the same day always compiles to the same minutes, which is what
    makes a scenario replayable.
    """
    entries: List[Dict[str, Any]] = []
    # Absolute minutes come from the clock itself, never from local arithmetic:
    # "day 1, 09:00" is `Clock.from_state`'s number, whatever origin the clock
    # uses. Computing it by hand is how a routine ends up firing on day 2.
    base = midnight(int(day), 0)
    for entity in getattr(world, "entities", []) or []:
        routine = getattr(entity, "schedule", None) or []
        for index, slot in enumerate(routine):
            start = _minutes_of_day(getattr(slot, "start", ""))
            if start is None:
                continue
            due = base + start
            entries.append(
                {
                    "kind": NPC_ROUTINE,
                    "due_abs_minute": due,
                    "priority": 50,
                    "payload": {
                        "npc_id": entity.id,
                        "activity": str(getattr(slot, "activity", "") or "idle"),
                        "location_id": str(getattr(slot, "location_id", "") or ""),
                        "start": str(getattr(slot, "start", "") or ""),
                        "minutes": max(0, int(getattr(slot, "minutes", 0) or 0)),
                        "day": int(day),
                        "slot_index": index,
                    },
                }
            )
    entries.sort(key=lambda item: (item["due_abs_minute"], item["payload"]["npc_id"]))
    return entries


def latest_slot_by_abs(
    world: WorldDefinition, npc_id: str, abs_minute: int
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """The slot that should be in force at `abs_minute`, and when it began.

    This is how a world "catches up" without replaying every minute it was
    absent: the last routine that started at or before the given minute *is* the
    NPC's current activity, so one answer replaces a whole day of ticks. Ties are
    broken by authored order, so a later slot in the same minute wins.
    """
    entity = next(
        (item for item in getattr(world, "entities", []) or [] if item.id == npc_id),
        None,
    )
    if entity is None:
        return None
    # Walk back until a day with a slot that has already started is found, so
    # the answer is "what has this NPC been doing", not "what is scheduled".
    best: Optional[Tuple[int, Dict[str, Any]]] = None
    for day in range(int(abs_minute // MINUTES_PER_DAY) + 2, 0, -1):
        for entry in routine_entries_for(entity, day):
            if entry["due_abs_minute"] <= int(abs_minute):
                if best is None or entry["due_abs_minute"] >= best[0]:
                    best = (entry["due_abs_minute"], entry["payload"])
        if best is not None:
            break
    return best


def routine_entries_for(entity: Any, day: int) -> List[Dict[str, Any]]:
    """One entity's routine slots for one day (same shape as `routine_entries`)."""
    # Absolute minutes come from the clock itself, never from local arithmetic:
    # "day 1, 09:00" is `Clock.from_state`'s number, whatever origin the clock
    # uses. Computing it by hand is how a routine ends up firing on day 2.
    base = midnight(int(day), 0)
    out: List[Dict[str, Any]] = []
    routine = getattr(entity, "schedule", None) or []
    for index, slot in enumerate(routine):
        start = _minutes_of_day(getattr(slot, "start", ""))
        if start is None:
            continue
        out.append(
            {
                "kind": NPC_ROUTINE,
                "due_abs_minute": base + start,
                "priority": 50,
                "payload": {
                    "npc_id": entity.id,
                    "activity": str(getattr(slot, "activity", "") or "idle"),
                    "location_id": str(getattr(slot, "location_id", "") or ""),
                    "start": str(getattr(slot, "start", "") or ""),
                    "minutes": max(0, int(getattr(slot, "minutes", 0) or 0)),
                    "day": int(day),
                    "slot_index": index,
                },
            }
        )
    return out


def schedule_day(
    store: Any, campaign_id: str, world: WorldDefinition, *, day: int, now_abs_minute: int
) -> List[Dict[str, Any]]:
    """Queue one day of routines, skipping slots that are already queued.

    Idempotent by construction: a slot is identified by (kind, npc, due), and the
    store answers `next_due_minute`/`due_events`, so a second call for the same
    day adds nothing. That matters because this runs on every action.
    """
    if store is None:
        return []
    existing = {
        (row.get("kind"), (row.get("payload") or {}).get("npc_id"), int(row["due_abs_minute"]))
        for row in store.list_scheduled_events(campaign_id, status="pending")
    }
    queued: List[Dict[str, Any]] = []
    for entry in routine_entries(world, day):
        key = (
            entry["kind"],
            entry["payload"]["npc_id"],
            int(entry["due_abs_minute"]),
        )
        if key in existing:
            continue
        if int(entry["due_abs_minute"]) < int(now_abs_minute):
            # Slots already in the past are not "future intentions": replaying
            # them would silently rewrite history the world has lived through.
            continue
        store.schedule_event(
            campaign_id,
            due_abs_minute=int(entry["due_abs_minute"]),
            kind=entry["kind"],
            payload=dict(entry["payload"]),
            priority=int(entry["priority"]),
            created_abs_minute=int(now_abs_minute),
        )
        existing.add(key)
        queued.append(entry)
    return queued


def days_ahead(store: Any, campaign_id: str, world: WorldDefinition, *, day: int, horizon: int = 1) -> None:
    """Keep the next `horizon` days scheduled.

    A world that is about to be left alone still has to come back to a coherent
    morning, so the routines are queued ahead instead of only for today.
    """
    now = midnight(int(day), 0)
    for offset in range(max(0, int(horizon)) + 1):
        schedule_day(store, campaign_id, world, day=int(day) + offset, now_abs_minute=now)


def due_slots(store: Any, campaign_id: str, until_abs_minute: int) -> List[Dict[str, Any]]:
    """Pending routine rows due by an instant (a thin filter over `due_events`)."""
    plan = plan_tick(store, campaign_id, until_abs_minute, max_events=200)
    return [entry for entry in plan.entries if entry.get("kind") == NPC_ROUTINE]


__all__ = [
    "MINUTES_PER_DAY",
    "NPC_ROUTINE",
    "days_ahead",
    "due_slots",
    "entity_id_of",
    "latest_slot_by_abs",
    "routine_entries",
    "routine_entries_for",
    "schedule_day",
]
