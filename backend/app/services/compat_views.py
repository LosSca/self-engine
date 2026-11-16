"""Compatibility views over the authoritative rules state.

**Status: transitional layer, not a feature.** This module exists only because
the frontend panels were written against the retired `/api/game` shapes and
have not been moved to `/api/v1` yet. It is a read-only projection: nothing is
stored here and nothing here is a source of truth.

What it is:

* `/api/game` no longer owns game state - every campaign plays through the
  rules engine (`ActionService`). What survives of the old surface are the
  *read* shapes the panels already speak: journal entries, crystal cards,
  inventory rows, NPC mind dossiers and map edges.
* Every projection is labelled `source="rules"`; anything still genuinely
  produced by the retired lane keeps `source="legacy"`, so a panel can tell
  them apart.
* A store that cannot answer degrades to an empty projection and reports the
  reason instead of raising into a panel.

Four rules keep the bridge honest:

1. nothing here writes state; a projection cannot change the game;
2. no new panel may be built against this module - use the `/api/v1` shapes;
3. when a panel learns to read `/api/v1` directly, delete its projection here
   in the same change, so this file only ever shrinks;
4. the legacy *lanes* are what is being retired, not the URLs. Migrating the
   stored data itself needs a human decision per campaign: old chat prose is
   not verified game fact, so it is never imported as authoritative state.

Consumers today: `backend/app/api/routes_game.py` only (grep `compat_views`).
Tests: `backend/tests/services/test_compat_views.py`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Event kinds that rebuild the readable timeline, in canonical order.
_TIMELINE_KINDS = ("PLAYER_ACTION", "NARRATOR_RESPONSE")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def timeline_messages(
    state_store=None,
    legacy_store=None,
    campaign_id: str = "",
) -> List[Dict[str, str]]:
    """The chat transcript: authoritative when the campaign has a state.

    A bound campaign is read from `GameStateStore.narrative_history`, which
    projects committed requests and their prose once per turn. Only an unbound
    (pre-migration) campaign falls back to the legacy event log - and that log
    is explicitly a record of what was said, not proof of what happened.
    """
    if state_store is not None and campaign_id:
        try:
            if state_store.get_state(campaign_id) is not None:
                return [
                    {
                        "role": "user" if event["event_type"] == "PLAYER_ACTION" else "assistant",
                        "content": event["payload"]["text"],
                    }
                    for event in state_store.narrative_history(campaign_id)
                ]
        except (KeyError, TypeError, ValueError, OSError) as exc:
            logger.warning("narrative history unavailable for %s: %s", campaign_id, exc)
    if legacy_store is None or not campaign_id:
        return []
    try:
        events = []
        for kind in _TIMELINE_KINDS:
            events.extend(legacy_store.get_by_type(campaign_id, kind))
        events.sort(key=lambda event: event.created_at)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        logger.warning("legacy timeline unavailable for %s: %s", campaign_id, exc)
        return []
    return [
        {
            "role": "user" if event.event_type == _TIMELINE_KINDS[0] else "assistant",
            "content": _text(event.payload.get("text")),
        }
        for event in events
        if _text(event.payload.get("text"))
    ]


def inventory_rows(state, world=None) -> List[Dict[str, Any]]:
    """Authoritative inventory in the row shape the inventory panel renders.

    The legacy engine had per-item `category`, `source` and `status` strings
    that nothing validated. The rules engine stores `item_id -> quantity`, so
    the row is built from the authored item definition and `status` is derived
    (`equipped` when the slot holds it, otherwise `carried`).
    """
    if state is None:
        return []
    definitions = {item.id: item for item in (getattr(world, "items", []) or [])}
    equipped = set((getattr(state, "equipment", {}) or {}).values())
    rows: List[Dict[str, Any]] = []
    for item_id, quantity in (getattr(state, "inventory", {}) or {}).items():
        if not quantity or quantity <= 0:
            continue
        definition = definitions.get(item_id)
        rows.append(
            {
                "id": item_id,
                "name": definition.name if definition is not None else item_id,
                "category": (definition.tags[0] if definition is not None and definition.tags else "misc"),
                "source": definition.description if definition is not None else "",
                "status": "equipped" if item_id in equipped else "carried",
                "quantity": quantity,
            }
        )
    rows.sort(key=lambda row: (row["status"] != "equipped", row["name"]))
    return rows


def journal_entries(state, world=None, limit: int = 100) -> List[Dict[str, Any]]:
    """A read-only journal derived from the authoritative state.

    The rules engine does not keep a prose journal (it keeps domain events), so
    the derived view reports the facts the state itself proves - quest stages
    reached, storyline nodes entered, the current place and clock - rather than
    inventing narrative. The legacy journal stays available for old campaigns.
    """
    if state is None:
        return []
    locations = {loc.id: loc.name for loc in (getattr(world, "locations", []) or [])}
    entries: List[Dict[str, Any]] = []
    for quest_id, progress in (getattr(state, "quests", {}) or {}).items():
        stage = getattr(progress, "stage", "")
        entries.append(
            {
                "category": "quest",
                "title": quest_id,
                "content": f"stage={stage}" if stage else "in progress",
                "source": "rules",
                "turn": getattr(state, "turn", 0),
            }
        )
    for node_id in (getattr(state, "story_nodes", []) or []):
        entries.append(
            {
                "category": "story",
                "title": node_id,
                "content": "storyline node entered",
                "source": "rules",
                "turn": getattr(state, "turn", 0),
            }
        )
    location_id = getattr(state, "location_id", "")
    if location_id:
        entries.append(
            {
                "category": "location",
                "title": locations.get(location_id, location_id),
                "content": f"day {getattr(state, 'day', 1)}",
                "source": "rules",
                "turn": getattr(state, "turn", 0),
            }
        )
    return entries[: max(0, limit)]


def memory_crystal_cards(legacy_store=None, campaign_id: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """Crystal cards for the memory panel.

    Crystals are produced by the narrator side effect in both lanes, so this is
    a read of stored crystals, not a projection: it is marked read-only and
    limited, and returns [] rather than raising when the store is unavailable.
    """
    if legacy_store is None or not campaign_id:
        return []
    getter = getattr(legacy_store, "get_crystals", None)
    if not callable(getter):
        return []
    try:
        crystals = getter(campaign_id, limit=limit) or []
    except (TypeError, ValueError, KeyError, OSError) as exc:
        logger.warning("memory crystals unavailable for %s: %s", campaign_id, exc)
        return []
    cards: List[Dict[str, Any]] = []
    for crystal in crystals:
        if isinstance(crystal, dict):
            cards.append(dict(crystal))
        else:
            as_dict = getattr(crystal, "as_dict", None)
            cards.append(as_dict() if callable(as_dict) else dict(vars(crystal)))
    return cards


def npc_mind_rows(minds: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    """Normalise stored NPC minds into the rows the mind panel renders."""
    rows: List[Dict[str, Any]] = []
    for mind in minds or []:
        if isinstance(mind, dict):
            rows.append(dict(mind))
            continue
        as_dict = getattr(mind, "as_dict", None)
        rows.append(as_dict() if callable(as_dict) else dict(vars(mind)))
    return rows


def graph_edges(relationships: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """The map view: existing nodes/links, unchanged shape.

    Kept named here so the endpoint does not have to know whether the graph is
    the legacy network or the rules-side projection: the shape is the contract.
    """
    return {"nodes": [], "links": list(relationships or [])}
