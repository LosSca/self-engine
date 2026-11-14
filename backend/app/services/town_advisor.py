"""The town's plan for what happens next - proposed, never asserted.

An LLM is good at "the miller is owed money, so he will go and argue about it at
the market tomorrow", and bad at being trusted with state. This module keeps
that split honest:

* the model only ever answers with *proposals*: a kind, a delay in game minutes
  and a small payload;
* a proposal becomes a row in the schedule and nothing else. It has to come due,
  it has to be dispatched to a handler, and the handler is the only thing that
  can touch the world - through the same effects every other event uses;
* a proposal may not invent a new kind of event. Kinds outside ALLOWED_KINDS are
  dropped, so a model cannot widen what the world is made of.

Everything here degrades to "the town has no plans" on any failure: an advisor
that is unreachable, slow or babbling must not stop a turn.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from app.engines import npc_schedule
from app.engines.game_clock import Clock

logger = logging.getLogger(__name__)

#: Kinds a proposal may ask for: exactly the kinds that have a handler in the
#: world. npc.routine is executed by the town's routine handler.
ALLOWED_KINDS = ("npc.routine",)

#: How far ahead a proposal may reach (one game day) and how many may land in
#: one turn. Both bound the plan, not the model's output length.
MAX_LOOKAHEAD_MINUTES = 24 * 60
MAX_PROPOSALS = 8

#: The planner is a hint, not a dependency: off unless an author asks for it.
ENV_FLAG = "ENGINE_TOWN_PLANNER"

_SYSTEM = (
    "You plan a small town. Answer with JSON only: a list of events, each "
    '{"kind": "<one of: npc.routine>", "in_minutes": <integer 1-1440>, '
    '"payload": {...}}. Propose at most 8. Never invent other fields, never '
    "describe narration, never speak for the player."
)

_FENCE = re.compile(r"^\s*\x60\x60\x60(?:json)?\s*(.+?)\s*\x60\x60\x60\s*$", re.S)


def enabled() -> bool:
    """Whether an author asked for model-driven planning in this world."""
    return str(os.environ.get(ENV_FLAG, "")).strip().lower() in ("1", "true", "yes", "on")


def _clock_of(state: Any) -> Optional[Clock]:
    try:
        return Clock.from_state(state)
    except (AttributeError, TypeError, ValueError) as exc:
        logger.debug("no clock on state: %s", exc)
        return None


def _town_prompt(state: Any, world: Any, now: Clock) -> str:
    people = [
        "- {0} ({1})".format(
            getattr(entity, "id", ""), getattr(entity, "name", "") or entity.id
        )
        for entity in getattr(world, "entities", []) or []
        if getattr(entity, "schedule", None)
    ][:12]
    lines = [
        "World: {0}".format(getattr(world, "name", "") or getattr(world, "world_id", "")),
        "Day {0}, {1:02d}:{2:02d}".format(now.day, now.minute // 60, now.minute % 60),
        "People with routines:",
    ]
    lines.extend(people or ["(none)"])
    lines.append("Propose what should happen next.")
    return "\n".join(lines)


def _strip_code_fences(text: str) -> str:
    body = (text or "").strip()
    fenced = _FENCE.match(body)
    return fenced.group(1) if fenced else body


def parse_proposals(raw: str) -> List[Dict[str, Any]]:
    """Proposals a model produced, filtered to what the world can execute.

    Never raises: malformed output means "no plan", which is a valid answer.
    """
    try:
        loaded = json.loads(_strip_code_fences(raw))
    except (TypeError, ValueError) as exc:
        logger.debug("town planner returned non-JSON: %s", exc)
        return []
    if isinstance(loaded, dict):
        loaded = loaded.get("events") if "events" in loaded else [loaded]
    if not isinstance(loaded, list):
        return []
    proposals: List[Dict[str, Any]] = []
    for item in loaded:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip()
        if kind not in ALLOWED_KINDS:
            logger.debug("dropping proposal with unknown kind %r", kind)
            continue
        try:
            delay = int(item.get("in_minutes", 0))
        except (TypeError, ValueError):
            continue
        if delay <= 0 or delay > MAX_LOOKAHEAD_MINUTES:
            continue
        payload = item.get("payload")
        payload = dict(payload) if isinstance(payload, dict) else {}
        proposals.append({"kind": kind, "in_minutes": delay, "payload": payload})
        if len(proposals) >= MAX_PROPOSALS:
            break
    return proposals


def _in_reach(proposal: Dict[str, Any]) -> bool:
    """Whether a proposal is worth writing down: future, and not absurdly far."""
    try:
        delay = int(proposal.get("in_minutes", 0))
    except (TypeError, ValueError):
        return False
    return 0 < delay <= MAX_LOOKAHEAD_MINUTES


def schedule_proposals(
    store: Any, campaign_id: str, state: Any, proposals: List[Dict[str, Any]], world: Any
) -> List[Dict[str, Any]]:
    """Put proposals in the future without acting on them.

    The clock decides when they happen; this only writes the intent down. A
    proposal about a person the world does not have is dropped, because the
    model is describing a name it made up.
    """
    now = _clock_of(state)
    if now is None or store is None:
        return []
    known = {
        npc_schedule.entity_id_of(entity)
        for entity in getattr(world, "entities", []) or []
    }
    written: List[Dict[str, Any]] = []
    for proposal in proposals:
        if not _in_reach(proposal):
            continue
        npc_id = str((proposal.get("payload") or {}).get("npc_id", ""))
        if npc_id and npc_id not in known:
            continue
        due = now.abs_minute + int(proposal["in_minutes"])
        store.schedule_event(
            campaign_id,
            due_abs_minute=due,
            kind=proposal["kind"],
            payload=dict(proposal.get("payload") or {}),
            priority=200,
            created_abs_minute=now.abs_minute,
        )
        written.append(dict(proposal, due_abs_minute=due))
    return written


async def _ask(llm: Any, prompt: str) -> str:
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]
    answer = llm.complete(messages)
    if hasattr(answer, "__await__"):
        answer = await answer
    return str(answer or "")


async def advise(service: Any, state: Any, llm: Any = None) -> List[Dict[str, Any]]:
    """Ask for the next plans and write them down as scheduled events.

    Returns the proposals actually scheduled. The caller's turn is already
    durable: a failure here is logged and dropped, never raised.
    """
    if llm is None or not enabled():
        return []
    try:
        store = getattr(service, "store", None)
        world = store.get_world(state.world_id) if store is not None else None
        if world is None:
            return []
        now = _clock_of(state)
        if now is None:
            return []
        proposals = parse_proposals(await _ask(llm, _town_prompt(state, world, now)))
        if not proposals:
            return []
        return schedule_proposals(store, state.campaign_id, state, proposals, world)
    except Exception as exc:  # noqa: BLE001 - planning is optional by construction
        logger.warning("town planner failed: %s", exc)
        return []


__all__ = [
    "ALLOWED_KINDS",
    "MAX_LOOKAHEAD_MINUTES",
    "MAX_PROPOSALS",
    "advise",
    "enabled",
    "parse_proposals",
    "schedule_proposals",
]
