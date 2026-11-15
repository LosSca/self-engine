"""Rebuilding a live session from persisted events.

A session outlives the process that created it: the API reloads a campaign on
the next request, and a rewind rewrites history and restarts it. Everything
that has to be recovered - the message history, the NPC minds, the plot lock,
the journal and the memory crystals - lives in the event store, and this module
is the only place that reads it back.

Turning a log back into state is where a codebase usually grows a 3000-line
class: every subsystem's restore logic accretes next to the subsystem itself.
Keeping the restores together instead makes the invariant explicit - *the event
log is the source of truth, every live structure is a projection of it* - and
makes it obvious when a new structure is added without a restore path.

The mixin relies on attributes created by :class:`GameSession.__init__`
(`_event_store`, `_memory`, `_journal`, `_npc_minds`, `_history`, ...). It is
not meant to be instantiated on its own: it is a partition of one class, not an
independent component.
"""
from __future__ import annotations

import logging

from app.db.event_store import EventType

logger = logging.getLogger(__name__)


class SessionRebuildMixin:
    """Restore live session structures from the event log."""

    def _rebuild_history_from_events(self) -> None:
        """Rebuild _history from persisted events so the AI retains context."""
        player_events = self._event_store.get_by_type(
            self.campaign_id, EventType.PLAYER_ACTION,
        )
        narrator_events = self._event_store.get_by_type(
            self.campaign_id, EventType.NARRATOR_RESPONSE,
        )
        all_events = player_events + narrator_events
        all_events.sort(key=lambda e: e.created_at)
        for ev in all_events:
            text = ev.payload.get("text", "")
            if not text:
                continue
            if ev.event_type == EventType.PLAYER_ACTION:
                self._history.append({"role": "user", "content": text})
            else:
                self._history.append({"role": "assistant", "content": text})
        self._turn_count = len(player_events)

    def _serialize_thoughts(self, mind) -> dict:
        """Camada 4 — persist value plus decay metadata so timing survives restarts."""
        out: dict[str, dict] = {}
        for k, t in mind.thoughts.items():
            out[k] = {
                "value": t.value,
                "created_at_turn": t.created_at_turn,
                "decay_after_turns": t.decay_after_turns,
            }
        return out

    def _rebuild_npc_minds_from_events(self) -> None:
        """Rebuild NPC minds from persisted NPC_THOUGHT events."""
        if not self._npc_minds:
            return
        thought_events = self._event_store.get_by_type(
            self.campaign_id, EventType.NPC_THOUGHT,
        )
        if not thought_events:
            return
        # Group by NPC name, keeping the latest thoughts per NPC
        latest_per_npc: dict[str, dict] = {}
        for ev in thought_events:
            name = ev.payload.get("name", "")
            if name:
                latest_per_npc[name] = ev.payload
        # Reconstruct minds — accept both legacy (str value) and Camada-4
        # (dict with value + decay metadata) thought shapes.
        for name, payload in latest_per_npc.items():
            mind = self._npc_minds._ensure_mind(self.campaign_id, name)
            for alias in payload.get("aliases", []):
                if alias.lower() not in [a.lower() for a in mind.aliases]:
                    mind.aliases.append(alias)
            for key, raw in payload.get("thoughts", {}).items():
                if isinstance(raw, dict):
                    value = raw.get("value")
                    if not value:
                        continue
                    mind.set_thought(
                        key,
                        str(value),
                        current_turn=int(raw.get("created_at_turn", 0) or 0),
                        decay_after_turns=raw.get("decay_after_turns"),
                    )
                elif raw:
                    mind.set_thought(key, str(raw))
        logger.info(
            "Rebuilt %d NPC minds from events for campaign %s",
            len(latest_per_npc), self.campaign_id,
        )

    def _rebuild_plot_lock_from_events(self) -> None:
        """Rebuild plot seeds, NPC seeds, micro-hooks, auto-plot counters,
        and lock from persisted PLOT_GENERATION events."""
        self._active_plot_seeds.clear()
        self._pending_micro_hook = ""
        self._pending_npc_seed = None
        self._pending_npc_introduced = False
        self._plot_pending = False
        self._plot_pending_since_turn = 0
        for state in self._auto_plot_state.values():
            state["last_turn"] = 0
            state["last_narrative_time"] = 0
            state["trigger_count"] = 0

        plot_events = self._event_store.get_by_type(
            self.campaign_id, EventType.PLOT_GENERATION,
        )
        if not plot_events:
            return

        player_events = self._event_store.get_by_type(
            self.campaign_id, EventType.PLAYER_ACTION,
        )

        # Rebuild pending details and auto-plot counters.
        for ev in plot_events:
            kind = ev.payload.get("kind", "")
            if kind == "micro_hook":
                text = ev.payload.get("data", {}).get("text", "")
                if text:
                    # Check if any narrator response after this event consumed it
                    narrator_events = self._event_store.get_by_type(
                        self.campaign_id, EventType.NARRATOR_RESPONSE,
                    )
                    consumed = any(
                        e.created_at > ev.created_at for e in narrator_events
                    )
                    if not consumed:
                        self._pending_micro_hook = text

            # Rebuild auto_plot_state counters per kind
            if kind in self._auto_plot_state:
                state = self._auto_plot_state[kind]
                state["trigger_count"] += 1
                # Derive last_turn: count player actions up to this event
                turns_at_event = sum(
                    1 for e in player_events if e.created_at <= ev.created_at
                )
                state["last_turn"] = turns_at_event

        # Restore pending NPC seed if the last plot event was an NPC
        # and it hasn't been introduced yet (name not found in subsequent narratives)
        last_plot = plot_events[-1]
        last_kind = last_plot.payload.get("kind", "")
        if last_kind == "npc":
            npc_data = last_plot.payload.get("data", {})
            npc_name = npc_data.get("name", "")
            if npc_name:
                narrator_events = self._event_store.get_by_type(
                    self.campaign_id, EventType.NARRATOR_RESPONSE,
                )
                introduced = any(
                    npc_name.lower() in (e.payload.get("text", "") or "").lower()
                    for e in narrator_events
                    if e.created_at > last_plot.created_at
                )
                if not introduced:
                    self._pending_npc_seed = npc_data
                    self._pending_npc_introduced = False
                    logger.info(
                        "Restored pending NPC seed '%s' from events for campaign %s",
                        npc_name, self.campaign_id,
                    )
                else:
                    self._pending_npc_introduced = True

        # Check lock: count player actions after the last plot
        turns_after_plot = sum(
            1 for e in player_events if e.created_at > last_plot.created_at
        )
        if turns_after_plot < self._PLOT_CONSUME_TURNS:
            self._plot_pending = True
            self._plot_pending_since_turn = self._turn_count - turns_after_plot
            if last_kind == "plot_arc":
                text = last_plot.payload.get("data", {}).get("text", "")
                if text:
                    self._active_plot_seeds[:] = [text]
        else:
            self._pending_micro_hook = ""
            self._pending_npc_seed = None
            self._pending_npc_introduced = False

    def rewind(self) -> None:
        """Fully rewind the last action: rebuild all in-memory state from events.

        After the route handler deletes the last event pair from the store,
        this method reconstructs history, NPC minds, journal, plot state,
        and memory crystals from the remaining events so that the game
        state is fully consistent.
        """
        # Reset all in-memory state
        self._history.clear()
        self._turn_count = 0
        if self._npc_minds:
            self._npc_minds._minds.pop(self.campaign_id, None)
        self._active_plot_seeds.clear()
        self._pending_micro_hook = ""
        self._pending_npc_seed = None
        self._pending_npc_introduced = False
        self._plot_pending = False
        self._plot_pending_since_turn = 0
        for state in self._auto_plot_state.values():
            state["last_turn"] = 0
            state["last_narrative_time"] = 0
            state["trigger_count"] = 0

        # Rebuild from remaining events
        self._rebuild_history_from_events()
        self._rebuild_npc_minds_from_events()
        self._rebuild_plot_lock_from_events()
        self._rebuild_journal_from_events()
        self._rebuild_memory_crystals()
        self._rebuild_player_power()
        self._rebuild_known_opponent_powers()

    def _rebuild_journal_from_events(self) -> None:
        """Rebuild the journal in-memory state from JOURNAL_ENTRY events in the store."""
        if not self._journal:
            return
        # Clear existing journal for this campaign
        self._journal._reset_journal(self.campaign_id, None)
        # Rebuild from persisted JOURNAL_ENTRY events
        from app.engines.journal_engine import JournalEntry, JournalCategory
        journal_events = self._event_store.get_by_type(
            self.campaign_id, EventType.JOURNAL_ENTRY,
        )
        if not journal_events:
            return
        entries = []
        for ev in journal_events:
            try:
                category = JournalCategory(ev.payload.get("category", ""))
                # Prefer the row-level witnessed_by (set on row insert);
                # fall back to payload for forward-compat with rows whose
                # witnessed_by lives only in the JSON blob.
                witnesses = list(ev.witnessed_by or ev.payload.get("witnessed_by", []))
                entries.append(JournalEntry(
                    campaign_id=self.campaign_id,
                    category=category,
                    summary=ev.payload.get("summary", ""),
                    created_at=ev.created_at,
                    witnessed_by=witnesses,
                ))
            except (ValueError, KeyError):
                continue
        if entries:
            self._journal._reset_journal(self.campaign_id, entries)
        logger.info(
            "Rebuilt %d journal entries from events for campaign %s",
            len(entries), self.campaign_id,
        )

    def _rebuild_memory_crystals(self) -> None:
        """Rebuild memory crystals from MEMORY_CRYSTAL events in the store.

        Handles the pyramid system: SHORT, MEDIUM, LONG, MEMORY tiers.
        Consumed state is INFERRED from tier counts (the DB payload is unreliable
        because consumed is only updated in-memory during consolidation).
        The cursor tracks the last SHORT crystal's timestamp for uncrystallized events.
        """
        from app.engines.memory_engine import (
            TIER_CONFIDENCE,
            CrystalTier,
            MemoryCrystal,
            derive_memory_id,
        )
        crystal_events = self._event_store.get_by_type(
            self.campaign_id, EventType.MEMORY_CRYSTAL,
        )
        crystals = []
        for ev in crystal_events:
            try:
                tier_str = ev.payload.get("tier", "SHORT")
                # Handle old "LONG" crystals from before the pyramid system
                # by mapping them to MEDIUM (closest equivalent)
                if tier_str == "LONG" and "consumed" not in ev.payload:
                    tier_str = "MEDIUM"
                tier = CrystalTier(tier_str)
                # witnessed_by may live in the payload (newer crystals) or be
                # missing entirely (legacy crystals from before Camada 3).
                # Missing → empty list, which means "not witnessed by anyone"
                # — safe default that excludes the crystal from per-NPC views.
                witnesses = list(ev.payload.get("witnessed_by", []))
                crystals.append(MemoryCrystal(
                    campaign_id=self.campaign_id,
                    tier=tier,
                    content=ev.payload.get("summary", ""),
                    ai_content=ev.payload.get("ai_content", ""),
                    event_count=ev.payload.get("event_count", 0),
                    consumed=False,  # Inferred below from tier counts
                    source_start_created_at=ev.payload.get("source_start_created_at"),
                    # Real raw-event boundary; fall back to persist time for
                    # legacy crystals that predate this field.
                    source_end_created_at=ev.payload.get("source_end_created_at") or ev.created_at,
                    witnessed_by=witnesses,
                    # Legacy crystals predate provenance; rebuild them with a
                    # derived id and the tier's default confidence rather than
                    # guessing a source that was never recorded.
                    memory_id=ev.payload.get("memory_id", ""),
                    source=ev.payload.get("source", ""),
                    observer=ev.payload.get("observer", "player"),
                    confidence=float(ev.payload.get("confidence") or 0.0),
                    importance=float(ev.payload.get("importance") or 0.0),
                    visibility=ev.payload.get("visibility", "player"),
                    related_entities=list(ev.payload.get("related_entities") or []),
                    status=ev.payload.get("status", "committed"),
                    # Theme-driven consolidation metadata (absent on crystals
                    # written before it existed, which keep empty defaults).
                    theme_key=ev.payload.get("theme_key", "") or "",
                    theme_keywords=list(ev.payload.get("theme_keywords") or []),
                    source_memory_ids=list(ev.payload.get("source_memory_ids") or []),
                ))
                if not crystals[-1].memory_id:
                    crystals[-1].memory_id = derive_memory_id(
                        self.campaign_id, tier.value,
                        crystals[-1].source_start_created_at,
                        crystals[-1].source_end_created_at,
                    )
                if not crystals[-1].confidence:
                    crystals[-1].confidence = TIER_CONFIDENCE.get(tier.value, 1.0)
            except Exception:
                continue

        # Infer consumed state.
        #
        # Theme-driven merges record the ids they replaced, so a rebuild uses
        # that record when it exists: "the Eileen thread merged these three" is
        # a fact, whereas "the first N of the tier were consumed" is a guess
        # that is only right for the old time-ordered cascade.
        consolidation_count = self._memory.CONSOLIDATION_COUNT
        merged_ids = {
            doc_id
            for c in crystals
            for doc_id in (c.source_memory_ids or [])
            if doc_id
        }
        for c in crystals:
            if c.memory_id and c.memory_id in merged_ids:
                c.consumed = True
        for current_tier in (CrystalTier.SHORT, CrystalTier.MEDIUM, CrystalTier.LONG):
            next_tier = current_tier.next_tier
            if next_tier is None:
                continue
            current_tier_crystals = [c for c in crystals if c.tier == current_tier]
            untracked = [c for c in current_tier_crystals if c.memory_id not in merged_ids]
            # Only fall back to the positional rule when nothing in this tier is
            # accounted for by an explicit merge record (legacy saves).
            if merged_ids and any(c.memory_id in merged_ids for c in current_tier_crystals):
                continue
            next_count = sum(1 for c in crystals if c.tier == next_tier)
            consumed_count = next_count * consolidation_count
            for c in untracked[:consumed_count]:
                c.consumed = True

        self._memory._crystals[self.campaign_id] = crystals
        # Cursor = last SHORT crystal's timestamp (for uncrystallized event tracking)
        short_crystals = [c for c in crystals if c.tier == CrystalTier.SHORT]
        if short_crystals:
            self._memory._last_crystal_cursor[self.campaign_id] = short_crystals[-1].source_end_created_at
        else:
            self._memory._last_crystal_cursor.pop(self.campaign_id, None)
