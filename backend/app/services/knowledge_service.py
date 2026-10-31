"""Knowledge loading and prompt assembly.

The depth chain decides what the narrator is allowed to know for a given turn:

* every enabled entry whose depth is within the configured cutoff is injected,
  ordered by (depth, sort_order, title) and grouped by layer;
* entries marked always_on bypass the cutoff, which is how small events stay
  present in free mode;
* storyline mode additionally boosts the knowledge referenced by every node the
  player has actually entered (trunk, attached branches and independent side
  lines alike), so a branch cannot silently lose its own knowledge.

The result is a validated KnowledgeContext. Rendering it into a prompt is a
separate, explicit step, so tests can assert exactly what the model would see.
"""
from __future__ import annotations

import math
import re
from typing import Dict, Iterable, List, Optional, Sequence

from app.engines.story_engine import validate_storyline
from app.models.knowledge import (
    LAYER_ORDER,
    ContextStats,
    ExcludedEntry,
    KnowledgeBlock,
    KnowledgeContext,
    KnowledgeEntry,
    PlayConfig,
    PlayMode,
    ScenarioPlayContext,
    Storyline,
)

DEFAULT_MAX_DEPTH = 9

_WORD_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[一-鿿]")


def _tokens(text: str) -> List[str]:
    """Words plus CJK characters and bigrams: a tiny sparse-vector tokenizer."""
    lowered = (text or "").casefold()
    words = _WORD_RE.findall(lowered)
    cjk = _CJK_RE.findall(lowered)
    bigrams = ["".join(pair) for pair in zip(cjk, cjk[1:])]
    return words + cjk + bigrams


def _cosine(query_tokens: List[str], doc_tokens: List[str], idf: Dict[str, float]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    q: Dict[str, float] = {}
    d: Dict[str, float] = {}
    for token in query_tokens:
        q[token] = q.get(token, 0.0) + 1.0
    for token in doc_tokens:
        d[token] = d.get(token, 0.0) + 1.0
    dot = sum(
        (count / len(query_tokens)) * (d[token] / len(doc_tokens)) * idf.get(token, 0.0)
        for token, count in q.items()
        if token in d
    )
    if dot <= 0:
        return 0.0
    q_norm = math.sqrt(
        sum((count / len(query_tokens)) ** 2 * idf.get(token, 0.0) ** 2 for token, count in q.items())
    )
    d_norm = math.sqrt(
        sum((count / len(doc_tokens)) ** 2 * idf.get(token, 0.0) ** 2 for token, count in d.items())
    )
    if q_norm == 0 or d_norm == 0:
        return 0.0
    return dot / (q_norm * d_norm)


def _layer_rank(layer: str) -> int:
    try:
        return LAYER_ORDER.index(layer)
    except ValueError:
        return len(LAYER_ORDER)


def build_context(
    entries: Sequence[KnowledgeEntry],
    mode: PlayMode = "free",
    max_depth: int = DEFAULT_MAX_DEPTH,
    story_node: Optional[str] = None,
    storyline: Optional[Storyline] = None,
    scenario_id: str = "",
    story_nodes: Optional[Sequence[str]] = None,
    focus: Optional[Sequence[str]] = None,
    budget_chars: int = 0,
    query: str = "",
) -> KnowledgeContext:
    """Select and order the knowledge entries visible for one turn.

    "story_nodes" is every node the campaign has actually entered; their
    knowledge is boosted together. "story_node" stays supported for callers that
    only know the active trunk node, and is used when "story_nodes" is omitted.

    "focus" names the current place, target or NPC; entries tagged with those
    names are ranked ahead of unrelated ones. "budget_chars" (>0) caps the
    injected title+content size, and every entry that no longer fits is reported
    in `stats.excluded` with the reason, so a large encyclopedia is never sent
    whole.
    """
    max_depth = max(0, min(9, int(max_depth)))
    query_text = " ".join([query or "", *(str(item) for item in (focus or []))]).strip()
    query_tokens = _tokens(query_text)
    focus_tokens = {
        str(item).strip().casefold() for item in (focus or []) if str(item).strip()
    }
    candidates: List[str] = []
    # Saved story history can outlive a switch to free mode. It must not
    # influence ranking, including for direct callers and context previews.
    if mode == "storyline":
        if story_nodes:
            candidates = [node_id for node_id in story_nodes if node_id]
        elif story_node:
            candidates = [story_node]
    else:
        story_node = None
    boost_ids: set = set()
    if storyline is not None and candidates:
        node_map = storyline.node_map()
        for candidate in candidates:
            node = node_map.get(candidate)
            if node is not None:
                boost_ids.update(node.knowledge_ids)

    blocks: List[KnowledgeBlock] = []
    excluded: List[ExcludedEntry] = []
    for entry in entries:
        reason = ""
        # Small ambient events stay switched on in every mode: a free-form
        # world still has things happening in it, and the author marked these
        # as always present rather than as storyline beats.
        always_present = entry.always_on and entry.layer == "event"
        if not entry.enabled:
            reason = "条目已停用"
        elif entry.audience_npc_ids and not (
            {npc.strip().casefold() for npc in entry.audience_npc_ids} & focus_tokens
        ):
            reason = "仅限特定 NPC 在场或成为行动目标时载入"
        elif mode == "free" and entry.mode_scope == "storyline" and not always_present:
            reason = "自由模式不载入故事线专属知识"
        elif mode == "storyline" and entry.mode_scope == "free":
            reason = "故事线模式不载入自由模式专属知识"
        elif entry.depth > max_depth and not entry.always_on:
            reason = (
                f"超出深度链上限（depth {entry.depth} > {max_depth}），"
                "如仍需载入请设为常驻"
            )
        if reason:
            excluded.append(
                ExcludedEntry(entry_id=entry.id, title=entry.title, reason=reason)
            )
            continue
        blocks.append(
            KnowledgeBlock(
                entry_id=entry.id,
                layer=entry.layer,
                title=entry.title,
                content=entry.content,
                tags=list(entry.tags),
                depth=entry.depth,
            )
        )
    doc_tokens = {
        entry.id: _tokens(" ".join([entry.title, entry.content, *entry.tags]))
        for entry in entries
    }
    document_count = max(1, len(entries))
    document_frequency: Dict[str, int] = {}
    for tokens in doc_tokens.values():
        for token in set(tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    idf = {
        token: math.log((document_count + 1) / (frequency + 1)) + 1.0
        for token, frequency in document_frequency.items()
    }
    scores = {
        entry_id: _cosine(query_tokens, tokens, idf)
        for entry_id, tokens in doc_tokens.items()
    }

    def _focus_hits(entry: KnowledgeEntry) -> int:
        if not focus_tokens:
            return 0
        return len({tag.strip().casefold() for tag in entry.tags} & focus_tokens)

    def _sort_key(block: KnowledgeBlock):
        entry = next(e for e in entries if e.id == block.entry_id)
        boosted = 0 if block.entry_id in boost_ids else 1
        return (
            boosted,
            -_focus_hits(entry),
            -round(scores.get(block.entry_id, 0.0), 6),
            _layer_rank(block.layer),
            entry.depth,
            entry.sort_order,
            block.title,
        )

    blocks.sort(key=_sort_key)

    if budget_chars and budget_chars > 0:
        kept: List[KnowledgeBlock] = []
        used = 0
        for block in blocks:
            cost = len(block.title) + len(block.content)
            if used + cost > budget_chars:
                excluded.append(
                    ExcludedEntry(
                        entry_id=block.entry_id,
                        title=block.title,
                        reason=f"超出上下文预算（{budget_chars} 字符）",
                    )
                )
                continue
            kept.append(block)
            used += cost
        blocks = kept

    counts: Dict[str, int] = {layer: 0 for layer in LAYER_ORDER}
    for block in blocks:
        counts[block.layer] = counts.get(block.layer, 0) + 1

    return KnowledgeContext(
        scenario_id=scenario_id,
        mode=mode,
        blocks=blocks,
        stats=ContextStats(
            mode=mode,
            total=len(entries),
            included=len(blocks),
            excluded=excluded,
            layer_counts=counts,
            max_depth=max_depth,
        ),
        story_node=story_node,
        story_nodes=list(candidates),
    )


def validate_play_config(
    config: PlayConfig,
    entries: Sequence[KnowledgeEntry],
    event_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    """Readable errors for a play configuration; empty means playable."""
    errors: List[str] = []
    knowledge_ids = {entry.id for entry in entries}
    known_events = set(event_ids) if event_ids is not None else None
    errors.extend(
        validate_storyline(config.storyline, knowledge_ids, known_events)
    )
    if config.mode == "storyline" and not config.storyline.nodes:
        errors.append("故事线模式至少需要一个故事线节点")
    return errors


def load_play_context(scenario_store, scenario_id: str) -> Optional[ScenarioPlayContext]:
    """Assemble the authored context of one scenario, or None if unknown."""
    if not scenario_id:
        return None
    scenario = scenario_store.get_scenario(scenario_id)
    if scenario is None:
        return None
    return ScenarioPlayContext(
        scenario_id=scenario_id,
        config=scenario_store.get_play_config(scenario_id),
        entries=scenario_store.list_knowledge(scenario_id),
    )


def describe_story_status(storyline: Storyline, entered: Sequence[str]) -> Dict[str, object]:
    """Compact, read-only summary for debug panels."""
    entered_set = set(entered)
    return {
        "name": storyline.name,
        "nodes": [
            {
                "id": node.id,
                "title": node.title,
                "kind": node.kind,
                "entered": node.id in entered_set,
            }
            for node in storyline.nodes
        ],
    }