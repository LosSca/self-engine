"""Narrator prompt blocks, taken from a ContextPack instead of hand-pasted (P4).

The narrator used to receive the same memory twice under two headings, each
one assembled inline in the turn handler: the crystal window said "WORLD
MEMORY", the journal said "STORY LOG", and the live NPC state said "NPC
STATES" - three sources of truth with three implicit priorities and no token
accounting. This module maps those three onto the pack's fixed order:

    current_state       live NPC state, inventory, the scene itself
    relevant_facts      high-confidence structured facts (when a store exists)
    relevant_episodes   the crystal window, then the journal

Rendering still produces the exact headings the narrator already reads, so
switching the pack on changes what is *kept under budget*, never what the
prompt calls it. That is what makes the flag safe to flip.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

from app.services.context_pack import (
    MODE_NARRATOR,
    ContextPack,
    build_context_pack,
    dedupe_items,
    estimate_tokens,
)

#: Default when the flag is on: the whole memory half of the narrator prompt.
DEFAULT_PACK_TOKENS = 6000

_HEADINGS = {
    "current_state": "NPC STATES (what each NPC is currently thinking/feeling):",
    "relevant_episodes": "WORLD MEMORY:",
    "relevant_facts": "VERIFIED FACTS (structured; higher authority than memory):",
}


def context_pack_enabled() -> bool:
    """MEMORY_CONTEXT_PACK=1 turns the pack on; anything else keeps the old path."""
    raw = os.environ.get("MEMORY_CONTEXT_PACK", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def pack_budget(context_window: int = 0) -> int:
    """Tokens for one narrator pack: an explicit env value, else a fraction.

    The fraction is deliberately small (memory is one block of a prompt that
    also carries the world, the history and the story cards) and the floor
    keeps a tiny context window from starving the pack completely.
    """
    explicit = os.environ.get("MEMORY_CONTEXT_PACK_TOKENS", "").strip()
    if explicit.isdigit() and int(explicit) > 0:
        return int(explicit)
    if context_window and context_window > 0:
        return max(1200, min(DEFAULT_PACK_TOKENS, int(context_window * 0.10)))
    return DEFAULT_PACK_TOKENS


def _fact_lines(facts: Sequence[Any]) -> List[Dict[str, str]]:
    """One line per fact, with its status visible so a guess cannot pass as fact."""
    out: List[Dict[str, str]] = []
    for fact in facts or ():
        subject = str(getattr(fact, "subject_id", "") or "").strip()
        predicate = str(getattr(fact, "predicate", "") or "").strip()
        value = str(getattr(fact, "object_value", "") or "").strip()
        if not (subject and predicate and value):
            continue
        status = str(getattr(fact, "status", "") or "")
        label = "fact" if status == "active" else (status or "unconfirmed")
        source = subject
        fact_id = str(getattr(fact, "id", "") or "")
        if fact_id:
            source = f"{subject} [{fact_id}]"
        out.append({"text": f"({label}) {subject} {predicate} {value}", "source": source})
    return out


def build_narrator_pack(
    campaign_id: str,
    *,
    memory_context: str = "",
    npc_states: str = "",
    inventory: str = "",
    journal: str = "",
    facts: Sequence[Any] = (),
    scene: str = "",
    token_budget: int = DEFAULT_PACK_TOKENS,
) -> ContextPack:
    """Assemble the narrator's memory pack from the blocks the caller already has.

    Nothing here decides authority: a fact arrives filtered by the same
    permission layer as everything else, and the live state simply sits above
    the recall in the render order so a stale summary cannot contradict it.
    """
    pack = build_context_pack(
        campaign_id,
        mode=MODE_NARRATOR,
        scene=scene,
        current_state=[block for block in (npc_states, inventory) if block and block.strip()],
        facts=facts,
        # Empty blocks are not items: an empty string rendered into the pack
        # produced "[source: memory]" lines with no text behind them.
        episodes=[block for block in (memory_context, journal) if block and block.strip()],
        token_budget=token_budget,
    )
    return pack


#: Characters per token, for converting the pack's token budget into the
#: character budget the context pack enforces. Mirrors estimate_tokens
#: (latin: four characters per token) so the two ceilings agree.
_CHARS_PER_TOKEN = 4


def _section_lines(pack: ContextPack, name: str) -> List[str]:
    """The lines render() actually kept for this section.

    Reading `section.items` would return every item the pack was *built* with -
    render() clips and drops items without mutating the source list, so the
    prompt would carry the very text the budget refused.
    """
    section = pack.sections.get(name)
    if section is None:
        return []
    kept = getattr(section, "kept", None)
    if kept is None:
        kept = [item.text for item in section.items]
    return [text for text in kept if text.strip()]


def render_narrator_pack(
    pack: ContextPack, *, budget: Optional[int] = None, _report: Any = None
) -> Dict[str, str]:
    """Render the pack into the three narrator blocks, honouring the budget.

    Returns the blocks the caller needs multiplied out - a missing block is an
    empty string, which is the same thing the old code produced when it had no
    content for it.
    """
    # The pack measures its budget in tokens and enforces it itself, so an
    # explicit override is the only thing passed through. The counts come back
    # from render_report because the blocks below must be the lines render()
    # kept: reading section.items would return the text the budget refused.
    if budget is None:
        _rendered, dropped, note_count = pack.render_report()
    else:
        _rendered, dropped, note_count = pack.render_report(budget=max(1, int(budget)))
    if _report is not None:
        # Optional out-parameter. The pack is rendered twice per turn (once to
        # size it, once for the prompt) and its notes accumulate, so a caller
        # reading notes afterwards would see every drop twice; note_count is
        # where this render started adding them.
        _report.update({"dropped": dropped, "note_count": note_count})
    facts = _section_lines(pack, "relevant_facts")
    episodes = _section_lines(pack, "relevant_episodes")
    state = _section_lines(pack, "current_state")
    out = {
        "facts": "",
        "episodes": "",
        "state": "",
        "sources": "",
        "used_tokens": str(pack.used_tokens),
    }
    if facts:
        out["facts"] = _HEADINGS["relevant_facts"] + "\n" + "\n".join(f"- {line}" for line in facts)
    if episodes:
        out["episodes"] = _HEADINGS["relevant_episodes"] + "\n" + "\n".join(episodes)
    if state:
        out["state"] = _HEADINGS["current_state"] + "\n" + "\n".join(state)
    sources = pack.section("sources").items
    if sources:
        out["sources"] = "\n".join(item.text for item in sources)
    return out


def pack_notes(pack: ContextPack) -> str:
    """One-line summary for the turn log: what the pack kept and what it dropped."""
    dropped = sum(section.dropped for section in pack.sections.values())
    used = pack.used_tokens
    budget = pack.token_budget
    return f"context pack: {used}/{budget} tokens, {dropped} dropped"


def for_prompt(pack: ContextPack, *, budget: Optional[int] = None) -> Dict[str, str]:
    """Build-and-render in one call, which is what the turn handler needs."""
    return render_narrator_pack(pack, budget=budget)


__all__ = [
    "DEFAULT_PACK_TOKENS",
    "build_narrator_pack",
    "context_pack_enabled",
    "estimate_tokens",
    "for_prompt",
    "pack_budget",
    "pack_notes",
    "render_narrator_pack",
]
