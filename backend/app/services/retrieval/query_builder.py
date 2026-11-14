"""Turn a game turn into a retrieval query.

The query is not the player's raw sentence: a sentence such as "我推开门，看看
外面有什么" contains verbs and particles that match nothing in the knowledge
base, while the *scene* - location, NPCs present, the object just mentioned -
is what the entries and crystals were written about.

The builder assembles weighted parts and reports them, so the debug panels can
show exactly what was searched, and so the evaluation harness can ablate one
part at a time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence


@dataclass(frozen=True)
class QueryPart:
    """One named fragment of a query and how many times it is repeated.

    Repetition is how a builder says "matters more". BM25 ignores repeated
    terms inside one query string, so the builder repeats the *fragment* and
    the caller expands it; :meth:`QueryParts.text` does that expansion.
    """

    name: str
    text: str
    weight: int = 1

    def expanded(self) -> str:
        value = (self.text or "").strip()
        if not value:
            return ""
        return " ".join([value] * max(1, self.weight))


@dataclass(frozen=True)
class QueryParts:
    """A built query: ordered parts, plus the flattened text and provenance."""

    parts: List[QueryPart] = field(default_factory=list)

    def text(self) -> str:
        return " ".join(p for p in (part.expanded() for part in self.parts) if p).strip()

    def names(self) -> List[str]:
        return [part.name for part in self.parts if part.text.strip()]

    def without(self, *names: str) -> "QueryParts":
        """Drop named parts: the evaluation harness ablates a part this way."""
        dropped = {n for n in names}
        return QueryParts(parts=[p for p in self.parts if p.name not in dropped])


def build_scene_query(
    player_input: str = "",
    location: str = "",
    focus: Sequence[str] = (),
    recent_prose: str = "",
    keywords: Sequence[str] = (),
    max_recent_chars: int = 400,
) -> QueryParts:
    """The query used for knowledge recall and crystal recall.

    Order matters only for the debug output; every fragment is flattened into
    one text before tokenizing.
    """
    parts: List[QueryPart] = []
    if player_input.strip():
        parts.append(QueryPart("input", player_input.strip(), weight=2))
    if location.strip():
        # The location is named in most knowledge entries and in almost every
        # crystal, so it is the single strongest signal available for free.
        parts.append(QueryPart("location", location.strip(), weight=2))
    for name in focus:
        if str(name or "").strip():
            parts.append(QueryPart("focus", str(name).strip(), weight=2))
    if keywords:
        joined = " ".join(str(k).strip() for k in keywords if str(k or "").strip())
        if joined:
            parts.append(QueryPart("keywords", joined, weight=2))
    if recent_prose.strip():
        parts.append(QueryPart("recent", recent_prose.strip()[-max_recent_chars:], weight=1))
    return QueryParts(parts=parts)


def build_memory_query(
    query_text: str = "",
    location: str = "",
    active_npcs: Iterable[str] = (),
    recent_events: str = "",
) -> QueryParts:
    """The query used for memory-crystal recall.

    Crystals are written by the model as compressed recaps, so they share
    vocabulary with the narrator prose rather than with the player's input;
    the recent event tail is therefore the most predictive part.
    """
    parts: List[QueryPart] = []
    if query_text.strip():
        parts.append(QueryPart("query", query_text.strip(), weight=1))
    if location.strip():
        parts.append(QueryPart("location", location.strip(), weight=2))
    for name in active_npcs:
        if str(name or "").strip():
            parts.append(QueryPart("npc", str(name).strip(), weight=2))
    if recent_events.strip():
        parts.append(QueryPart("events", recent_events.strip()[-400:], weight=1))
    return QueryParts(parts=parts)


def build_graph_query(
    location: str = "",
    active_npcs: Iterable[str] = (),
    player_input: str = "",
    mentioned_entities: Iterable[str] = (),
) -> QueryParts:
    """The query used for the graph channel.

    A graph lookup needs *entities*, not a paragraph: Graphiti matches on the
    subject/object names it indexed. Passing a narration tail (the old
    behaviour) matched nothing, which is why the graph channel rarely fired.
    """
    parts: List[QueryPart] = []
    for name in active_npcs:
        if str(name or "").strip():
            parts.append(QueryPart("npc", str(name).strip(), weight=1))
    for name in mentioned_entities:
        if str(name or "").strip():
            parts.append(QueryPart("entity", str(name).strip(), weight=1))
    if location.strip():
        parts.append(QueryPart("location", location.strip(), weight=1))
    if player_input.strip() and not parts:
        parts.append(QueryPart("input", player_input.strip(), weight=1))
    return QueryParts(parts=parts)
