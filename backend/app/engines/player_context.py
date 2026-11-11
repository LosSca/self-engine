"""Layered context for the agents (Phase 2).

One place decides what a model is allowed to know about the current moment, so
the narrative and system agents cannot drift apart and no caller can dump the
whole world into a prompt. Layers, from always-present to on-demand:

* ``scene``    - where the player is, what is here, the immediate stakes.
* ``player``   - inventory, quests, currency, level, flags worth knowing.
* ``npc``      - the NPC being addressed: dossier, mood, mood history.
* ``memory``   - what this actor is allowed to remember (witness-based).
* ``canon``    - authored knowledge for the current node/location.
* ``history``  - the recent committed turns.

Every field carries its source (``facts`` vs ``beliefs`` vs ``guesses``), so a
model can never mistake an NPC opinion for a world fact. The builder is pure
read-only: it never mutates the state or the world it reads.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.engines.npc_dossier import NpcDossier, dossier_for
from app.models.game import GameState, WorldDefinition

logger = logging.getLogger(__name__)

#: Caps keep one verbose location from eating the whole prompt.
MAX_VISIBLE_ITEMS = 12
MAX_VISIBLE_ENTITIES = 12
MAX_INVENTORY_LINES = 20
MAX_QUEST_LINES = 10
MAX_HISTORY_LINES = 6
#: Total characters a rendered context may spend; the long tail is dropped
#: first, so the mandatory layers always survive.
MAX_CONTEXT_CHARS = 6000


@dataclass
class ContextSection:
    """One labeled block of context with its provenance.

    ``kind`` is one of ``fact`` (authoritative state), ``canon`` (authored
    world knowledge), ``belief`` (what a particular NPC thinks), ``guess``
    (a model proposal, never authority) or ``history`` (past turns).
    """

    key: str
    title: str
    kind: str = "fact"
    lines: List[str] = field(default_factory=list)
    optional: bool = False

    def render(self) -> str:
        if not self.lines:
            return ""
        header = f"[{self.title} | {self.kind}]"
        return "\n".join([header, *[f"- {line}" for line in self.lines]])


@dataclass
class PlayerContext:
    """The assembled context for one agent call."""

    campaign_id: str = ""
    location_id: str = ""
    npc_id: str = ""
    sections: List[ContextSection] = field(default_factory=list)
    #: Facts a narrator must not contradict (already committed).
    facts: Dict[str, Any] = field(default_factory=dict)
    #: The dossier of the NPC this call is about, when there is one.
    npc: Optional[NpcDossier] = None

    def add(
        self,
        key: str,
        title: str,
        lines: Sequence[str],
        *,
        kind: str = "fact",
        optional: bool = False,
    ) -> None:
        cleaned = [str(line).strip() for line in lines if str(line).strip()]
        if cleaned:
            self.sections.append(
                ContextSection(
                    key=key, title=title, kind=kind, lines=cleaned, optional=optional
                )
            )

    def section(self, key: str) -> Optional[ContextSection]:
        for item in self.sections:
            if item.key == key:
                return item
        return None

    def render(self, budget: int = MAX_CONTEXT_CHARS) -> str:
        """Render within a character budget, dropping optional layers first."""
        mandatory = [s.render() for s in self.sections if not s.optional]
        optional = [s.render() for s in self.sections if s.optional]
        out = [text for text in mandatory if text]
        used = sum(len(text) + 1 for text in out)
        for text in optional:
            if not text:
                continue
            if used + len(text) + 1 > budget:
                continue
            out.append(text)
            used += len(text) + 1
        return "\n\n".join(out)

    def kinds(self) -> Dict[str, str]:
        """Section key -> evidence kind, for diagnostics and tests."""
        return {s.key: s.kind for s in self.sections}


def _name(world: WorldDefinition, item_id: str) -> str:
    for item in world.items:
        if item.id == item_id:
            return item.name or item_id
    return item_id


def _location(world: WorldDefinition, location_id: str):
    for location in world.locations:
        if location.id == location_id:
            return location
    return None


def _entity(world: WorldDefinition, entity_id: str):
    for entity in world.entities:
        if entity.id == entity_id:
            return entity
    return None


def _alive(state: GameState, world: WorldDefinition, entity_id: str) -> bool:
    record = state.entities.get(entity_id)
    if record is not None:
        return bool(record.alive)
    entity = _entity(world, entity_id)
    return bool(entity is not None and entity.kind != "object")


def _present(world: WorldDefinition, state: GameState) -> List[str]:
    ids: List[str] = []
    for entity in world.entities:
        record = state.entities.get(entity.id)
        here = record.location_id if record is not None and record.location_id else entity.location_id
        if here == state.location_id and _alive(state, world, entity.id):
            ids.append(entity.id)
    location = _location(world, state.location_id)
    if location is not None:
        for npc_id in location.npc_ids:
            if npc_id not in ids and _entity(world, npc_id) is None:
                ids.append(npc_id)
    return ids


class PlayerContextBuilder:
    """Build the layered context for one agent call.

    ``memory_provider`` is optional and duck-typed: anything exposing
    ``build_npc_knowledge_window(campaign_id, npc_name)`` works, which is the
    existing MemoryEngine method. A memory failure degrades to "no memory
    layer", never to a failed turn.
    """

    def __init__(self, memory_provider: Any = None, history_provider: Any = None):
        self.memory_provider = memory_provider
        self.history_provider = history_provider

    # ------------------------------------------------------------------ build
    def build(
        self,
        world: WorldDefinition,
        state: GameState,
        *,
        npc_id: str = "",
        knowledge: Optional[Sequence[Dict[str, Any]]] = None,
        history: Optional[Sequence[str]] = None,
        include_history: bool = True,
        budget: int = MAX_CONTEXT_CHARS,
    ) -> PlayerContext:
        context = PlayerContext(
            campaign_id=state.campaign_id,
            location_id=state.location_id,
            npc_id=npc_id,
        )
        location = _location(world, state.location_id)
        if location is not None:
            context.add("scene", "Scene", [f"{location.name}: {location.description or '(no description)'}"])
        else:
            context.add("scene", "Scene", [f"Location {state.location_id} (undefined)"])
        here = _present(world, state)
        if here:
            labels = []
            for entity_id in here[:MAX_VISIBLE_ENTITIES]:
                entity = _entity(world, entity_id)
                labels.append(entity.name if entity is not None else entity_id)
            context.add("present", "Present", labels)
        visible = [
            _name(world, item_id)
            for item_id, where in state.item_locations.items()
            if where == state.location_id
        ]
        if visible:
            context.add("items_here", "Items here", visible[:MAX_VISIBLE_ITEMS])

        inventory = [
            f"{_name(world, item_id)} x{count}"
            for item_id, count in state.inventory.items()
        ]
        player_lines = [f"Turn {state.turn}", f"Currency {state.currency}", f"Level {state.level} (xp {state.xp})"]
        if inventory:
            player_lines.extend(inventory[:MAX_INVENTORY_LINES])
        context.add("player", "Player", player_lines)

        quest_lines = []
        for quest in world.quests:
            progress = state.quests.get(quest.id)
            if progress is None:
                continue
            objectives = ", ".join(
                f"{o.description or o.id} {progress.objectives.get(o.id, 0)}/{o.required_count}"
                for o in quest.objectives
            )
            quest_lines.append(f"{quest.title} [{progress.status}] {objectives}")
        context.add("quests", "Quests", quest_lines[:MAX_QUEST_LINES], optional=True)

        if npc_id:
            entity = _entity(world, npc_id)
            dossier = dossier_for(world, state, npc_id)
            if dossier is not None:
                context.npc = dossier
                context.add(
                    "npc_identity",
                    f"NPC {dossier.name}",
                    dossier.identity_lines(),
                    kind="fact",
                )
                context.add(
                    "npc_state",
                    f"NPC {dossier.name} state",
                    dossier.state_lines(),
                    kind="belief",
                )
                knowledge_lines = dossier.knowledge_lines(world)
                if knowledge_lines:
                    context.add(
                        "npc_knows",
                        f"What {dossier.name} knows",
                        knowledge_lines,
                        kind="belief",
                    )
                if dossier.secrets:
                    context.add(
                        "npc_secrets",
                        f"What {dossier.name} hides",
                        dossier.secrets,
                        kind="belief",
                    )
                if entity is not None and entity.dialogue:
                    context.add(
                        "npc_voice",
                        f"{dossier.name} phrasing",
                        list(entity.dialogue[:3]),
                        kind="canon",
                        optional=True,
                    )
                memory = self._memory(state, entity)
                if memory:
                    context.add(
                        "npc_memory",
                        f"{dossier.name} remembers",
                        memory,
                        kind="history",
                        optional=True,
                    )

        canon_lines = [
            str(block.get("content") or "").strip()
            for block in knowledge or ()
            if block.get("kind") == "canon" and str(block.get("content") or "").strip()
        ]
        context.add("canon", "World canon", canon_lines, kind="canon", optional=True)

        history_lines = [str(line).strip() for line in history or () if str(line).strip()]
        history_lines.extend(
            str(block.get("content") or "").strip()
            for block in knowledge or ()
            if block.get("kind") == "history" and str(block.get("content") or "").strip()
        )
        if include_history and history_lines:
            context.add(
                "history",
                "Recent turns",
                history_lines[-MAX_HISTORY_LINES:],
                kind="history",
                optional=True,
            )

        context.facts = {
            "location_id": state.location_id,
            "visible_entities": here,
            "visible_items": [k for k, v in state.item_locations.items() if v == state.location_id],
            "open_doors": [door_id for door_id, is_open in state.doors.items() if is_open],
            "turn": state.turn,
        }
        # Rendering here once surfaces budget problems in the caller's logs
        # rather than in a silent truncation by the provider.
        rendered = context.render(budget=budget)
        if len(rendered) > budget:
            logger.debug("context for %s still over budget (%s chars)", state.campaign_id, len(rendered))
        return context

    # ----------------------------------------------------------------- memory
    def _memory(self, state: GameState, entity: Any) -> List[str]:
        if self.memory_provider is None or entity is None:
            return []
        window = ""
        try:
            window = self.memory_provider.build_npc_knowledge_window(
                state.campaign_id, getattr(entity, "name", "") or getattr(entity, "id", "")
            )
        except Exception:  # noqa: BLE001 - memory is advisory
            logger.warning("npc memory window failed", exc_info=True)
            return []
        lines: List[str] = []
        for raw in (window or "").splitlines():
            text = raw.strip()
            if not text or text.startswith("==="):
                continue
            lines.append(text)
        return lines


def build_player_context(
    world: WorldDefinition,
    state: GameState,
    *,
    npc_id: str = "",
    memory_provider: Any = None,
    knowledge: Optional[Sequence[Dict[str, Any]]] = None,
    history: Optional[Sequence[str]] = None,
    include_history: bool = True,
) -> PlayerContext:
    """One-shot convenience wrapper around PlayerContextBuilder."""
    builder = PlayerContextBuilder(memory_provider=memory_provider)
    return builder.build(
        world,
        state,
        npc_id=npc_id,
        knowledge=knowledge,
        history=history,
        include_history=include_history,
    )
