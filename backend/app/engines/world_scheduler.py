"""World time advances on its own, and the events it passes are executed.

Phase 2 of docs/hybrid-world-simulation.md. Two things were missing before this
module existed:

* the clock only moved *because the player typed something* - nobody could wait
  for a shop to open, because "09:00" was not an instant the engine could move to;
* there was nowhere to put "this is going to happen at 18:00". Every domain
  event was a fact that had already occurred, so a schedule could only be
  re-derived, never remembered.

This module adds neither a clock nor an event log. It is a pure planner over
`GameState.day/minute` plus the `scheduled_events` table that
`GameStateStore` owns:

* `plan_tick(store, campaign_id, now)` answers "what is due by `now`", in causal
  order, without touching anything;
* `run_tick(store, state, plan, handlers)` applies them to a copy of the state;
* `commit_tick` writes the advanced clock, the produced domain events and the
  "fired" flags in one transaction.

Nothing here decides whether an action is *allowed* - that stays in the rules
engine, reached through `ActionService` when a due event actually does something.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.db.event_store import EventType
from app.engines.game_clock import MINUTES_PER_DAY, Clock, midnight, split
from app.models.game import GameState

logger = logging.getLogger(__name__)

#: What a tick handler returns: the new state, the domain events it produced and
#: any events it wants to leave for the future.
TickResult = Tuple[GameState, List[Dict[str, Any]], List[Dict[str, Any]]]

#: `kind` -> handler(store, state, entry) -> TickResult | None. The handler may be
#: sync or async; `run_tick` awaits whatever it returns.
TickHandler = Callable[[Any, GameState, Dict[str, Any]], Any]


@dataclass
class TickPlan:
    """Everything that is due by a given instant, in order.

    `capped` means the plan hit `max_events`; the caller should tick again
    rather than assume the world is caught up.
    """

    now_abs_minute: int
    entries: List[Dict[str, Any]] = field(default_factory=list)
    capped: bool = False

    @property
    def empty(self) -> bool:
        return not self.entries


@dataclass
class TickOutcome:
    """The result of a committed tick."""

    from_abs_minute: int
    to_abs_minute: int
    fired: List[Dict[str, Any]] = field(default_factory=list)
    state_version: int = 0
    request_id: str = ""

    @property
    def minutes_advanced(self) -> int:
        return max(0, self.to_abs_minute - self.from_abs_minute)


def clock_of(state: GameState) -> Clock:
    return Clock.from_state(state)


def plan_tick(
    store: Any, campaign_id: str, now_abs_minute: int, max_events: int = 100
) -> TickPlan:
    """Due events up to `now_abs_minute`, ordered by (due, priority, sequence).

    Pure with respect to the world: it reads the store and returns a plan. It
    marks nothing, advances nothing, fires nothing.
    """
    due = store.due_events(campaign_id, int(now_abs_minute), limit=max_events + 1)
    capped = len(due) > max_events
    if capped:
        due = due[:max_events]
    return TickPlan(now_abs_minute=int(now_abs_minute), entries=due, capped=capped)


def add_minutes(clock: Clock, minutes: int) -> int:
    """Absolute minute `minutes` after `clock`."""
    return clock.abs_minute + max(0, int(minutes or 0))


async def run_tick(
    store: Any,
    state: GameState,
    plan: TickPlan,
    handlers: Dict[str, TickHandler],
    clock_paused: bool = False,
    from_abs_minute: Optional[int] = None,
    to_abs_minute: Optional[int] = None,
) -> Tuple[GameState, List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Execute a plan against `state`, returning the new state and the record.

    Returns `(state, domain_events, new_scheduled, fired_ids)`. The state is a
    copy: nothing is committed here.

    `from_abs_minute` / `to_abs_minute` are the interval this tick *covers*. When
    they are given the resulting clock is exactly `to_abs_minute` - the caller is
    spending real time, so the events and the clock advance together. Without
    them the clock is left where it was (the caller only wanted the events).

    A handler that raises is recorded as a failed `WORLD_TICK` and the tick
    continues: one broken schedule must not stop the world, but it must also not
    disappear silently.
    """
    new_state = state.model_copy(deep=True)
    events: List[Dict[str, Any]] = []
    scheduled: List[Dict[str, Any]] = []
    fired_ids: List[str] = []

    for entry in plan.entries:
        # The dispatch key is the row's own `kind` (what the schedule is), not a
        # field inside its payload: an event with an empty payload is still a
        # schedule that has to run.
        handler = handlers.get(entry.get("kind", ""))
        due = int(entry["due_abs_minute"])
        if handler is not None:
            try:
                produced = handler(store, new_state, entry)
                if hasattr(produced, "__await__"):
                    produced = await produced
            except Exception as exc:  # noqa: BLE001 - a schedule is authored data
                logger.warning(
                    "scheduled event %s (%s) failed: %s",
                    entry.get("id"), entry.get("kind"), exc, exc_info=True,
                )
                fired_ids.append(entry["id"])
                events.append(
                    _event(
                        EventType.WORLD_TICK,
                        {
                            "scheduled_id": entry.get("id"),
                            "kind": entry.get("kind"),
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                            "due_abs_minute": due,
                        },
                    )
                )
                continue
            if produced is not None:
                new_state, extra_events, extra_scheduled = produced
                events.extend(extra_events)
                scheduled.extend(extra_scheduled)
        fired_ids.append(entry["id"])
        events.append(
            _event(
                EventType.WORLD_TICK,
                {
                    "scheduled_id": entry.get("id"),
                    "kind": entry.get("kind"),
                    "payload": entry.get("payload", {}),
                    "due_abs_minute": due,
                    "status": "fired",
                },
            )
        )

    if to_abs_minute is not None and not clock_paused:
        target = split(int(to_abs_minute))
        current = Clock.from_state(new_state)
        if Clock(day=target[0], minute=target[1]).abs_minute > current.abs_minute:
            new_state.day, new_state.minute = target
    if from_abs_minute is not None and not events:
        logger.debug(
            "tick %s -> %s fired nothing", from_abs_minute, to_abs_minute
        )
    return new_state, events, scheduled, fired_ids


def commit_tick(
    store: Any,
    campaign_id: str,
    state: GameState,
    *,
    from_abs_minute: int,
    to_abs_minute: int,
    events: List[Dict[str, Any]],
    fired_ids: List[str],
    new_scheduled: Iterable[Dict[str, Any]] = (),
    expected_version: Optional[int] = None,
) -> TickOutcome:
    """Persist one tick: new clock, its domain events, and the fired flags.

    `expected_version` defaults to the version `state` already carries, which is
    the version the caller read. A concurrent writer makes this raise
    `StateConflictError`, exactly like any action commit.
    """
    request_id = f"tick-{uuid.uuid4().hex}"
    advanced = state.model_copy(deep=True)
    advanced.version = state.version + 1
    advanced.updated_at = _now()
    result = {
        "request_id": request_id,
        "campaign_id": campaign_id,
        "from_abs_minute": int(from_abs_minute),
        "to_abs_minute": int(to_abs_minute),
        "fired": len(fired_ids),
    }
    store.commit_simulation_tick(
        campaign_id=campaign_id,
        request_id=request_id,
        request_payload={
            "campaign_id": campaign_id,
            "request_id": request_id,
            "action": "world_tick",
            "actor_type": "system",
            "actor_id": "world",
            "params": {
                "from_abs_minute": int(from_abs_minute),
                "to_abs_minute": int(to_abs_minute),
            },
        },
        result_payload=result,
        state=advanced,
        events=list(events),
        fired_event_ids=list(fired_ids),
        expected_version=state.version if expected_version is None else expected_version,
    )
    for entry in new_scheduled:
        store.schedule_event(
            campaign_id,
            due_abs_minute=int(entry["due_abs_minute"]),
            kind=entry.get("kind", ""),
            payload=dict(entry.get("payload") or {}),
            priority=int(entry.get("priority", 100)),
            created_abs_minute=int(to_abs_minute),
        )
    return TickOutcome(
        from_abs_minute=int(from_abs_minute),
        to_abs_minute=int(to_abs_minute),
        fired=list(events),
        state_version=advanced.version,
        request_id=request_id,
    )


def catch_up_minute(store: Any, campaign_id: str, now_abs_minute: int) -> int:
    """Where the next tick should stop: the next due event, or `now`.

    This is the whole answer to "do not poll every minute": with nothing
    scheduled the clock jumps straight to the requested instant in one hop, and
    with something due it jumps only that far first, so the event is applied at
    its own minute and causality holds.
    """
    nxt = store.next_due_minute(campaign_id)
    if nxt is None or nxt > int(now_abs_minute):
        return int(now_abs_minute)
    return int(nxt)


def _event(kind: EventType, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"event_type": kind.value, "payload": payload}


def _now() -> str:
    return datetime.utcnow().isoformat()


__all__ = [
    "MINUTES_PER_DAY",
    "TickOutcome",
    "TickPlan",
    "add_minutes",
    "catch_up_minute",
    "clock_of",
    "commit_tick",
    "midnight",
    "plan_tick",
    "run_tick",
]
