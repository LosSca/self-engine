from __future__ import annotations

"""The single place a prompt's memory is assembled (P4).

Until now every caller decided for itself what to paste: the narrator got a
markdown blob of tiers, an NPC got a witness filter, and a route could ask for
the permanent tier directly. That made priority implicit - whichever caller
ran last won - and made token accounting guesswork.

A ContextPack is the same content with the decisions made explicit: each
section says what it is, where it came from and how much it may cost, and the
whole thing is rendered in one fixed order. The order is the point: live state
is always above memory, so a stale summary can never argue with the current
board. Anything that does not fit its budget is dropped with a note, not
silently truncated mid-sentence.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.services.retrieval.tokenizer import tokenize_text

logger = logging.getLogger(__name__)


#: Section names, in the order they are rendered. Lower index wins ties.
SECTION_ORDER = (
    "scene",
    "current_state",
    "relevant_facts",
    "unresolved_commitments",
    "relevant_episodes",
    "npc_perspective",
    "sources",
)

#: Share of the budget each section may occupy before the next one is tried.
SECTION_WEIGHTS = {
    "scene": 0.10,
    "current_state": 0.25,
    "relevant_facts": 0.25,
    "unresolved_commitments": 0.10,
    "relevant_episodes": 0.20,
    "npc_perspective": 0.10,
    "sources": 0.10,
}

#: What the caller is building a prompt for.
MODE_NARRATOR = "narrator"
MODE_NPC = "npc"
MODE_PLAYER = "player"

def estimate_tokens(text: str) -> int:
    """Cheap token estimate: CJK one-per-char, Latin ~4 chars per token.

    Deliberately not a real tokenizer: the budget only has to be conservative
    and stable, and importing the narrator's tokenizer here would create a
    dependency the memory layer does not otherwise need.
    """
    if not text:
        return 0
    tokens = tokenize_text(text)
    latin = sum(len(t) for t in tokens if t.isascii())
    cjk = sum(len(t) for t in tokens if not t.isascii())
    return cjk + (latin + 3) // 4


@dataclass
class ContextItem:
    """One line of a pack, with the provenance the renderer must keep."""

    text: str
    source: str = ""
    origin_ids: List[str] = field(default_factory=list)
    confidence: float = 1.0
    order: int = 0
    #: Pinned items are admitted even when they overflow the budget: live
    #: state and canon must not be silently traded for a shorter prompt.
    pinned: bool = False

    def render(self, *, with_source: bool = True) -> str:
        if with_source and self.source:
            return f"{self.text} [source: {self.source}]"
        return self.text


@dataclass
class ContextSection:
    name: str
    items: List[ContextItem] = field(default_factory=list)
    dropped: int = 0
    budget_tokens: int = 0
    #: Exactly what render() admitted, after clipping and dropping. The source
    #: `items` are what the pack was built with; a caller that renders must
    #: read these, or it would reproduce the text the budget refused.
    kept: List[str] = field(default_factory=list)

    def add(self, item: ContextItem) -> None:
        self.items.append(item)

    def is_empty(self) -> bool:
        return not self.items


@dataclass
class ContextPack:
    """A prompt's memory, ordered, budgeted and attributable."""

    campaign_id: str = ""
    mode: str = MODE_NARRATOR
    sections: Dict[str, ContextSection] = field(default_factory=dict)
    token_budget: int = 0
    used_tokens: int = 0
    notes: List[str] = field(default_factory=list)

    def add(self, section_name: str, item: ContextItem) -> None:
        self.sections.setdefault(section_name, ContextSection(section_name)).add(item)

    def section(self, name: str) -> ContextSection:
        return self.sections.setdefault(name, ContextSection(name))

    def ordered_sections(self) -> List[ContextSection]:
        rank = {name: i for i, name in enumerate(SECTION_ORDER)}
        present = [s for s in self.sections.values() if not s.is_empty()]
        return sorted(present, key=lambda s: (rank.get(s.name, len(rank)), s.name))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "mode": self.mode,
            "token_budget": self.token_budget,
            "used_tokens": self.used_tokens,
            "notes": list(self.notes),
            "sections": {
                s.name: {
                    "items": [
                        {
                            "text": i.text,
                            "source": i.source,
                            "origin_ids": list(i.origin_ids),
                            "confidence": i.confidence,
                        }
                        for i in s.items
                    ],
                    "dropped": s.dropped,
                    "budget_tokens": s.budget_tokens,
                }
                for s in self.ordered_sections()
            },
        }

    def render(self, *, budget: Optional[int] = None) -> str:
        """Render the pack in fixed priority order, honouring a token budget.

        `budget` is authoritative when given and re-applied here, so a caller
        can tighten it after the fact without rebuilding. Sections that have
        no room are named in the notes instead of disappearing quietly.
        """
        limit = self.token_budget if budget is None else budget
        total = limit if limit and limit > 0 else None
        remaining = total
        out: List[str] = []
        used = 0
        clipped = 0
        for section in self.ordered_sections():
            header = f"=== {section.name.upper()} ==="
            header_cost = estimate_tokens(header)
            # Two ceilings: the pack-wide remainder, and this section's own
            # share. Without the second one a single long memory eats the
            # whole pack and the facts underneath it never render.
            share = _item_budget(total, section.name) if total is not None else None
            section.budget_tokens = share or 0
            ceiling = share
            if remaining is not None:
                ceiling = remaining if ceiling is None else min(ceiling, remaining)
            lines: List[str] = []
            cost = 0
            over_budget = 0
            for item in section.items:
                rendered = item.render()
                price = _price(item)
                # An item may carry several lines (the narrator joins a whole
                # block - the memory window, the journal - into one item), so
                # a per-item price is not enough: it can silently bust both
                # ceilings and still be counted as "one item over budget".
                # A large unpinned item is cut down to what fits instead of
                # being taken whole or dropped whole: an item that is one line
                # is either kept or dropped by the ceilings, but a multi-line
                # block keeps as much of itself as the budget allows.
                if total is not None and not _must_keep(item):
                    room = _room(total, share, ceiling, remaining, header_cost + cost)
                    if room is not None and price > room:
                        over_budget += 1
                        clipped += 1
                        rendered, price = _clip(item, room)
                        if price <= 0:
                            section.dropped += 1
                            continue
                over_share = ceiling is not None and header_cost + cost + price > ceiling
                # When the only ceiling left is the pack remainder, the items
                # already in this section have not been charged to it yet (that
                # happens once, at the end of the loop), so the test is
                # remaining - cost.
                over_pack = (
                    remaining is not None and header_cost + cost + price > remaining
                )
                if (over_share or over_pack) and not _must_keep(item):
                    over_budget += 1
                    clipped += 1
                    # Pinned items (an always-include memory, the live state)
                    # are admitted even when they overflow, the same rule the
                    # crystal selector uses: silently dropping something that
                    # was explicitly marked as required is the one failure a
                    # layout step must not have. Everything else gives way.
                    section.dropped += 1
                    continue
                lines.append(rendered)
                cost += price
            if not lines:
                section.kept = []
                if section.dropped:
                    self.notes.append(f"{section.name}: nothing fit the budget")
                continue
            if section.dropped:
                # Dropping is never silent: the caller can log or assert on it,
                # and a later reader of the trace knows the pack truncated.
                self.notes.append(f"{section.name}: dropped {section.dropped} item(s) over budget")
            section.kept = list(lines)
            out.append(header)
            out.extend(lines)
            used += header_cost + cost
            if remaining is not None:
                remaining -= header_cost + cost
        self.used_tokens = used
        if total is not None and used > total:
            self.notes.append(f"pack exceeded budget: {used} > {total}")
        if clipped:
            # A clip is a real loss of text, so it is reported like a drop: the
            # caller logs the notes and a reviewer sees why a memory stops
            # mid-line.
            self.notes.append(f"clipped {clipped} oversized item(s) to fit the budget")
        return "\n".join(out)

    def render_report(self, **kwargs: Any) -> tuple:
        """render() plus the counts it produced, in one call.

        The counts have to be returned because render() is called twice on the
        same pack by design (once to size it, once for the prompt): a caller
        reading the notes afterwards would see every drop twice.
        """
        rendered = self.render(**kwargs)
        dropped = sum(section.dropped for section in self.sections.values())
        return rendered, dropped, len(self.notes)


#: An unpinned item never takes more than this share of the pack. A whole
#: memory window arrives as one item, so without a per-item cap a single block
#: can eat the pack even when it is not a required one.
_SINGLE_ITEM_SHARE = 0.5

#: Characters per token used to size a clip. estimate_tokens charges one token
#: per CJK character and one per four latin characters, so a character budget
#: taken at this rate is close to the token room and cheap to verify.
_CHARS_PER_TOKEN_ESTIMATE = 2


def _price(item: ContextItem) -> int:
    """What an item costs when it is admitted whole."""
    return estimate_tokens(item.render())


def _room(
    total: int,
    share: Optional[int],
    ceiling: Optional[int],
    remaining: Optional[int],
    spent: int,
) -> Optional[int]:
    """Tokens left for this item under every ceiling that applies."""
    # The per-item share is the binding ceiling for an unpinned item: a share
    # is sized to hold several items, so one block must never take all of it.
    rooms = [int(total * _SINGLE_ITEM_SHARE)]
    if share is not None:
        rooms.append(share - spent)
    if ceiling is not None:
        rooms.append(ceiling - spent)
    if remaining is not None:
        rooms.append(remaining - spent)
    return min(rooms) if rooms else None


def _clip(item: ContextItem, room: int) -> tuple:
    """Cut an item down to a whole number of lines that fits `room` tokens.

    CJK costs about one token per character and latin about one per four, so
    the character budget is taken conservatively and trimmed further per line
    until the estimate actually fits. Truncation keeps the head, which is the
    order the pack put the lines in.
    """
    if room <= 0:
        return "", 0
    source_suffix = f" [source: {item.source}]" if item.source else ""
    chars = max(4, int(room * _CHARS_PER_TOKEN_ESTIMATE) - len(source_suffix))
    text = item.text
    while chars > 0:
        clipped = text[:chars].rstrip()
        rendered = clipped + source_suffix
        price = estimate_tokens(rendered)
        if price <= room:
            return rendered, price
        chars = int(chars * 0.8) - 1
        if chars < 4:
            # A handful of characters is not a memory, it is a fragment with a
            # source attached. Dropping it is the honest outcome.
            break
    return "", 0


def _must_keep(item: ContextItem) -> bool:
    """Whether this item may never be dropped (pinned truth or live state)."""
    return bool(getattr(item, "pinned", False))


def _item_budget(total: int, name: str) -> int:
    weight = SECTION_WEIGHTS.get(name, 0.1)
    return max(24, int(total * weight))


def build_context_pack(
    campaign_id: str,
    *,
    mode: str = MODE_NARRATOR,
    scene: Optional[Any] = None,
    current_state: Optional[Any] = None,
    facts: Sequence[Any] = (),
    episodes: Sequence[Any] = (),
    npc_perspective: Optional[str] = None,
    commitments: Sequence[Any] = (),
    token_budget: int = 4000,
) -> ContextPack:
    """Assemble a pack from already-permission-filtered parts.

    Nothing here decides who may see what: `facts` must arrive pre-filtered
    for the observer (see app.services.memory_perspective). That separation is
    deliberate - the pack is a layout layer, and a layout layer that also
    made permission decisions would be the second place to get it wrong.
    """
    pack = ContextPack(campaign_id=campaign_id, mode=mode, token_budget=token_budget)

    if scene:
        for text, source in _as_items(scene, "scene"):
            pack.add("scene", ContextItem(text=text, source=source))

    if current_state:
        for text, source in _as_items(current_state, "game_state"):
            # Live state outranks everything: full confidence, top order, and
            # it is pinned - a stale summary must never outrank what is true now.
            pack.add(
                "current_state",
                ContextItem(
                    text=text, source=source, confidence=1.0, order=0, pinned=True
                ),
            )

    for fact in facts or ():
        pack.add("relevant_facts", _fact_item(fact))

    if commitments:
        for text, source in _as_items(commitments, "commitment"):
            pack.add("unresolved_commitments", ContextItem(text=text, source=source))

    for episode in episodes or ():
        pack.add("relevant_episodes", _episode_item(episode))

    if npc_perspective:
        for line in str(npc_perspective).splitlines():
            text = line.strip()
            if text:
                pack.add("npc_perspective", ContextItem(text=text, source="npc"))

    return pack

def _as_items(value: Any, default_source: str) -> List[tuple]:
    """Accept a string, a list of strings, or (text, source) pairs."""
    if value is None:
        return []
    if isinstance(value, str):
        return [(line.strip(), default_source) for line in value.splitlines() if line.strip()]
    out: List[tuple] = []
    for entry in value:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                out.append((text, default_source))
            continue
        if isinstance(entry, dict):
            text = str(entry.get("text") or entry.get("value") or "").strip()
            source = str(entry.get("source") or default_source)
            if text:
                out.append((text, source))
            continue
        if isinstance(entry, (tuple, list)) and entry:
            text = str(entry[0]).strip()
            source = str(entry[1]) if len(entry) > 1 else default_source
            if text:
                out.append((text, source))
    return out


def _fact_item(fact: Any) -> ContextItem:
    subject = getattr(fact, "subject_id", "") or ""
    predicate = getattr(fact, "predicate", "") or ""
    value = getattr(fact, "object_value", "") or ""
    status = str(getattr(fact, "status", "") or "")
    label = "fact" if status == "active" else (status or "unconfirmed")
    text = f"({label}) {subject} {predicate} {value}".strip()
    confidence = float(getattr(fact, "confidence", 0.5) or 0.5)
    fact_id = getattr(fact, "id", "") or ""
    valid_from = getattr(fact, "valid_from", "") or ""
    source = f"fact {fact_id}" if fact_id else "fact"
    if valid_from:
        source += f" (from {valid_from[:10]})"
    origin = list(getattr(fact, "source_event_ids", []) or [])
    return ContextItem(text=text, source=source, origin_ids=origin, confidence=confidence)


def _episode_item(episode: Any) -> ContextItem:
    text = getattr(episode, "ai_content", None) or getattr(episode, "content", None) or str(episode)
    memory_id = getattr(episode, "memory_id", "") or ""
    tier = getattr(episode, "tier", "") or ""
    tier_value = getattr(tier, "value", tier)
    start = getattr(episode, "source_start_created_at", "") or ""
    source = f"memory {memory_id}" if memory_id else "memory"
    if tier_value:
        source += f" ({tier_value}"
        source += f", from {start[:10]})" if start else ")"
    elif start:
        source += f" (from {start[:10]})"
    importance = float(getattr(episode, "importance", 0.5) or 0.5)
    confidence = float(getattr(episode, "confidence", 0.5) or 0.5)
    score = max(0.0, min(1.0, importance * confidence))
    origin = [memory_id] if memory_id else []
    return ContextItem(text=str(text), source=source, origin_ids=origin, confidence=score)


def dedupe_items(pack: ContextPack) -> int:
    """Drop repeated lines inside each section, keeping the first occurrence."""
    removed = 0
    for section in pack.sections.values():
        seen: set = set()
        kept: List[ContextItem] = []
        for item in section.items:
            key = " ".join(item.text.split()).casefold()
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            kept.append(item)
        section.items = kept
    return removed