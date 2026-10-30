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
) -> KnowledgeContext:
    """Select and order the knowledge entries visible for one turn.

    "story_nodes" is every node the campaign has actually entered; their
    knowledge is boosted together. "story_node" stays supported for callers that
    only know the active trunk node, and is used when "story_nodes" is omitted.
    """
    max_depth = max(0, min(9, int(max_depth)))
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

    def _sort_key(block: KnowledgeBlock):
        entry = next(e for e in entries if e.id == block.entry_id)
        boosted = 0 if block.entry_id in boost_ids else 1
        return (boosted, _layer_rank(block.layer), entry.depth, entry.sort_order, block.title)

    blocks.sort(key=_sort_key)
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