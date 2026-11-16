"""Knowledge loading and prompt assembly.

The depth chain decides what the narrator is allowed to know for a given turn:

* every enabled entry whose depth is within the configured cutoff is injected,
  ordered by (depth, sort_order, title) and grouped by layer;
* entries marked always_on bypass the cutoff, which is how small events stay
  present in free mode;
* storyline mode additionally boosts the knowledge referenced by every node the
  player has actually entered (trunk, attached branches and independent side
  lines alike), so a branch cannot silently lose its own knowledge;
* storyline mode also boosts the knowledge carried by event packs that are
  currently armed - the ambient events an author switched on for this point of
  the story.

The result is a validated KnowledgeContext. Rendering it into a prompt is a
separate, explicit step, so tests can assert exactly what the model would see.
"""
from __future__ import annotations

import logging
import os
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from app.engines.story_engine import validate_storyline
from app.services.retrieval.query_builder import build_scene_query
from app.services.retrieval.reranker import build_signals
from app.services.retrieval.retrieval_service import RetrievalService
from app.services.retrieval.tokenizer import tokenize_text
from app.models.game import GameState, WorldDefinition
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

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 9

#: The lexical channel is a plug: BM25 today, anything else later. Ranking is
#: no longer done here, so a deployment can swap the retriever without editing
#: this module, and the evaluation harness can compare two retrievers on the
#: same corpus.
_LEXICAL_RETRIEVER = "bm25"

#: Builds a semantic channel for one corpus. Injected rather than imported so
#: this module stays importing-neither-network-nor-model, and a caller that has
#: no embedding backend simply passes None.
VectorRetrieverFactory = Callable[
    [Sequence[str], Sequence[str]], Optional[object]
]


#: Builds the graph channel for one scene. Takes the campaign id, the scene text
#: and the names already on stage, and returns anything with a synchronous
#: `search(query) -> Sequence[RetrievedDoc]`. Injected for the same reason as the
#: vector channel: this module must not import neo4j, graphiti or a driver.
GraphRetrieverFactory = Callable[[str, str, Sequence[str]], Optional[object]]


def default_vector_retriever_factory(
    documents: Sequence[str], doc_ids: Sequence[str], metadata: Optional[Sequence[Dict[str, object]]] = None
):
    """The process-wide semantic channel, or None when embeddings are off.

    Kept lazy: importing an embedding backend at module import time would make
    every process pay for a feature most of them do not use, and would make the
    tests depend on an optional package.
    """
    if not os.environ.get("ENGINE_EMBEDDING_PROVIDER", "").strip() and not (
        os.environ.get("OPENAI_API_KEY", "").strip()
        or os.environ.get("CUSTOM_LLM_API_KEY", "").strip()
    ):
        return None
    from app.services.retrieval_embedding import build_vector_channel

    return build_vector_channel(documents, doc_ids, metadata)


def _tokens(text: str) -> List[str]:
    """Tokens of one text, using the shared mixed-script tokenizer."""
    return tokenize_text(text)


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
    armed_packs: Optional[Sequence[str]] = None,
    focus: Optional[Sequence[str]] = None,
    budget_chars: int = 0,
    query: str = "",
    vector_retriever: Optional[VectorRetrieverFactory] = None,
    graph_retriever: Optional[GraphRetrieverFactory] = None,
    campaign_id: str = "",
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
    pack_candidates: List[str] = [pack_id for pack_id in (armed_packs or []) if pack_id]
    if storyline is not None and candidates:
        node_map = storyline.node_map()
        for candidate in candidates:
            node = node_map.get(candidate)
            if node is not None:
                boost_ids.update(node.knowledge_ids)
    if storyline is not None and pack_candidates:
        pack_map = storyline.pack_map()
        for pack_id in pack_candidates:
            pack = pack_map.get(pack_id)
            if pack is not None and pack.enabled:
                boost_ids.update(pack.knowledge_ids)

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
    # Only the entries that survived the filters are ranked: a disabled or
    # out-of-scope entry can neither be injected nor influence the IDF weights
    # of the ones that can. The retriever owns scoring; this function owns the
    # order the author asked for, which the reranker applies as a tiebreak.
    by_id: Dict[str, KnowledgeEntry] = {entry.id: entry for entry in entries}
    ranked_ids = [block.entry_id for block in blocks]
    documents = [
        " ".join([by_id[entry_id].title, by_id[entry_id].content, *by_id[entry_id].tags])
        for entry_id in ranked_ids
    ]
    focus_names = [str(item) for item in (focus or [])]
    scene_query = build_scene_query(
        player_input=query or "",
        focus=focus_names,
    )
    # The semantic channel is offered, not assumed: `vector_retriever` is None
    # whenever no embedding backend is configured, and the pipeline then runs
    # lexically and says so in `stats.retrieval_degraded`.
    vector_channel = None
    if vector_retriever is not None and documents:
        vector_channel = vector_retriever(
            documents,
            ranked_ids,
            metadata=[
                {"tags": list(by_id[entry_id].tags), "title": by_id[entry_id].title}
                for entry_id in ranked_ids
            ],
        )
    # The graph channel is offered on the same terms as the semantic one: None
    # whenever no temporal graph is reachable, and a channel that fails lands in
    # `trace.degraded` instead of failing the turn.
    graph_channel = None
    if graph_retriever is not None and campaign_id and documents:
        try:
            graph_channel = graph_retriever(campaign_id, scene_query.text(), focus_names)
        except (RuntimeError, OSError, ValueError, TypeError, KeyError, ImportError):
            logger.warning("graph channel could not be built", exc_info=True)
            graph_channel = None
    fused, trace = RetrievalService().retrieve(
        documents,
        ranked_ids,
        scene_query,
        signals=build_signals(
            pinned=[entry_id for entry_id in ranked_ids if entry_id in boost_ids],
            focus=focus_names,
            depth={entry_id: by_id[entry_id].depth for entry_id in ranked_ids},
            layer_rank={block.entry_id: _layer_rank(block.layer) for block in blocks},
        ),
        vector=vector_channel,
        graph=graph_channel,
        final_limit=max(1, len(ranked_ids)),
    )
    retriever = fused
    # Entries without a retrieval hit still belong in the list (the depth chain,
    # not the query, decides visibility); they rank below every hit, in the
    # author's own order. A zero-hit context is a legitimate state - an empty
    # query, or a world whose entries share no word with the scene.
    if fused:
        order = [doc.doc_id for doc in fused]
        missing = [entry_id for entry_id in ranked_ids if entry_id not in set(order)]
        missing.sort(key=lambda entry_id: (
            0 if entry_id in boost_ids else 1,
            _layer_rank(by_id[entry_id].layer),
            by_id[entry_id].depth,
            by_id[entry_id].sort_order,
            by_id[entry_id].title,
        ))
        order = order + missing
    else:
        order = sorted(
            ranked_ids,
            key=lambda entry_id: (
                0 if entry_id in boost_ids else 1,
                _layer_rank(by_id[entry_id].layer),
                by_id[entry_id].depth,
                by_id[entry_id].sort_order,
                by_id[entry_id].title,
            ),
        )
    blocks_by_id = {block.entry_id: block for block in blocks}
    blocks = [blocks_by_id[entry_id] for entry_id in order]
    #: Provenance of each block, so a debugger can tell "ranked by BM25" from
    #: "kept only because the depth chain wanted it".
    retrieval_provenance = {doc.doc_id: doc.provenance for doc in fused}

    if budget_chars and budget_chars > 0:
        kept: List[KnowledgeBlock] = []
        used = 0
        always_on_ids = {entry.id for entry in entries if entry.always_on}
        for block in blocks:
            cost = len(block.title) + len(block.content)
            if used + cost > budget_chars and block.entry_id not in always_on_ids:
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
            retrieval_channels=dict(trace.channels),
            retrieval_degraded=[
                *trace.degraded,
                *([_LEXICAL_RETRIEVER] if ranked_ids and not trace.channels else []),
                *(
                    ["vector:not_configured"]
                    if ranked_ids and vector_retriever is None
                    else []
                ),
            ],
        ),
        story_node=story_node,
        story_nodes=list(candidates),
        armed_packs=list(pack_candidates),
    )

def armed_pack_ids(
    storyline: Optional[Storyline],
    state: Optional[GameState],
    world: Optional[WorldDefinition],
) -> List[str]:
    """Armed event packs for this state, or [] when there is nothing to arm.

    Kept here so the preview endpoint and the action pipeline answer the same
    question with the same code.
    """
    if storyline is None or state is None or world is None:
        return []
    from app.engines.story_engine import pack_status

    armed, _fired = pack_status(storyline, state, world)
    return armed


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