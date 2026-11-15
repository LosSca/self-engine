"""Hybrid retrieval over memory crystals and structured facts (P2).

The old crystal recall scored every crystal in Python against one bag of
query keywords and kept the top N per tier. Two things were wrong with that:
it could not express "these two names are the same person", and it could not
use anything but the current tier, so a fact from chapter two vanished the
moment its crystal was consolidated away.

This module keeps the scoring the project already ships as one channel, and
adds the channels the rescue-a-knight query needs next to it:

    query -> entity resolution
         -> bm25 (crystal text)    \
         -> alias (resolved names)  >-> RRF -> provenance rerank -> budget
         -> temporal (facts valid at a time) /

Everything optional degrades: the alias channel is empty when the entity table
is empty, the fact channel is empty when the fact store is missing, and the
engine own score is still applied, so an install with no memory.db behaves
exactly as it does today.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from app.services.retrieval import lexical_retriever
from app.services.retrieval.entity_index import (
    EntityIndex,
    find_mentions,
    mentioned_names,
)
from app.services.retrieval.fusion import reciprocal_rank_fusion
from app.services.retrieval.models import FusionConfig, RetrievedDoc
from app.services.retrieval.reranker import (
    DEFAULT_RERANK_WEIGHTS,
    RerankSignals,
    RerankWeights,
    rerank,
)

logger = logging.getLogger(__name__)

#: Channel names, kept short because they end up inside provenance strings.
CHANNEL_BM25 = "bm25"
CHANNEL_ALIAS = "alias"
CHANNEL_ENGINE = "engine"
CHANNEL_FACT = "fact"
CHANNEL_VECTOR = "vector"


@dataclass
class CrystalDoc:
    """One crystal, flattened into the fields retrieval needs.

    The engine owns MemoryCrystal; this is the retrieval-side view, so a test
    can build documents without constructing an engine.
    """

    doc_id: str
    text: str
    tier: str = "SHORT"
    consumed: bool = False
    entities: List[str] = field(default_factory=list)
    witnesses: List[str] = field(default_factory=list)
    confidence: float = 1.0
    importance: float = 0.0
    source_event_ids: List[str] = field(default_factory=list)
    start: str = ""
    end: str = ""
    always_include: bool = False

    def as_meta(self) -> Dict[str, object]:
        return {
            "tier": self.tier,
            "consumed": self.consumed,
            "entities": list(self.entities),
            "witnesses": list(self.witnesses),
            "confidence": self.confidence,
            "importance": self.importance,
            "always_include": self.always_include,
            "start": self.start,
            "end": self.end,
        }


@dataclass
class FactDoc:
    """One structured fact, as the temporal channel sees it."""

    doc_id: str
    text: str
    subject_id: str = ""
    predicate: str = ""
    object_value: str = ""
    confidence: float = 0.5
    status: str = ""
    valid_from: str = ""
    valid_until: str = ""
    observers: List[str] = field(default_factory=list)

    def as_text(self) -> str:
        return f"{self.subject_id} {self.predicate} {self.object_value}".strip()


@dataclass
class HybridResult:
    """What the hybrid layer produced, and why.

    documents keeps the fused ranking; the trace fields record the channels
    that ran, how many candidates each produced and anything that degraded, so
    a debug panel or an evaluation run can explain a ranking without
    re-running it.
    """

    documents: List[RetrievedDoc] = field(default_factory=list)
    query: str = ""
    entities: List[str] = field(default_factory=list)
    channels: Dict[str, int] = field(default_factory=dict)
    degraded: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def doc_ids(self) -> List[str]:
        return [doc.doc_id for doc in self.documents]

    def as_dict(self) -> Dict[str, object]:
        return {
            "query": self.query,
            "entities": list(self.entities),
            "channels": dict(self.channels),
            "degraded": list(self.degraded),
            "errors": list(self.errors),
            "documents": [doc.doc_id for doc in self.documents],
        }

def _alias_channel(
    query: str,
    docs: Sequence[CrystalDoc],
    index: EntityIndex,
    *,
    limit: int,
) -> List[RetrievedDoc]:
    """Rank crystals that mention an entity the query resolves to.

    This is the channel that answers "that knight who rescued me" when the
    player never uses her name: the query names a kind of person, the scene
    mentions her, and the index links the two by entity rather than by shared
    words.
    """
    if not index.aliases:
        return []
    wanted = {name.casefold() for name in mentioned_names(query, index)}
    if not wanted:
        return []
    scored: List[RetrievedDoc] = []
    for doc in docs:
        hits = 0
        for entity in doc.entities or ():
            canonical = index.canonical(entity)
            if canonical and canonical.casefold() in wanted:
                hits += 1
        for mention in find_mentions(doc.text or "", index):
            if mention.canonical_name.casefold() in wanted:
                hits += 1
        if hits:
            scored.append(
                RetrievedDoc(
                    doc_id=doc.doc_id,
                    score=float(hits),
                    channel=CHANNEL_ALIAS,
                    text=doc.text,
                    metadata=doc.as_meta(),
                    provenance=f"alias:{hits}",
                )
            )
    scored.sort(key=lambda doc: -doc.score)
    return scored[:limit]


def _engine_channel(
    docs: Sequence[CrystalDoc],
    scores: Dict[str, float],
    *,
    limit: int,
) -> List[RetrievedDoc]:
    """The engine own scene score, turned into a ranked channel.

    Keeping it as a channel rather than a post-filter is what lets the old
    ranking still win when there is no alias or temporal signal: fusion ranks
    by position, and this list is ordered exactly as the legacy scorer
    ordered it.
    """
    if not scores:
        return []
    by_id = {doc.doc_id: doc for doc in docs}
    ordered = sorted(scores.items(), key=lambda item: -item[1])
    out: List[RetrievedDoc] = []
    for doc_id, score in ordered[:limit]:
        doc = by_id.get(doc_id)
        if doc is None:
            continue
        out.append(
            RetrievedDoc(
                doc_id=doc_id,
                score=float(score),
                channel=CHANNEL_ENGINE,
                text=doc.text,
                metadata=doc.as_meta(),
                provenance=f"engine:{round(float(score), 3)}",
            )
        )
    return out


def _fact_channel(
    query: str,
    facts: Sequence[FactDoc],
    index: EntityIndex,
    *,
    limit: int,
) -> List[RetrievedDoc]:
    """Structured facts the query names, ranked by confidence.

    Facts are matched on the entity they are about as well as on their text,
    because a stored fact reads "Eileen | relationship | friend" while the
    query reads the Chinese spelling. The validity window is applied by the
    caller (the store hands over facts valid at the requested instant), so
    this channel is purely about relevance.
    """
    if not facts:
        return []
    wanted = {name.casefold() for name in mentioned_names(query, index)}
    lowered_query = str(query or "").casefold()
    query_terms = [term for term in lowered_query.split() if len(term) > 1]
    scored: List[RetrievedDoc] = []
    for fact in facts:
        hit = 0.0
        subject = index.canonical(fact.subject_id) if fact.subject_id else ""
        if subject and subject.casefold() in wanted:
            hit += 2.0
        text = fact.as_text().casefold()
        if any(term in text for term in query_terms):
            hit += 1.0
        if fact.object_value and fact.object_value.casefold() in lowered_query:
            hit += 1.0
        if hit <= 0.0:
            continue
        scored.append(
            RetrievedDoc(
                doc_id=fact.doc_id,
                score=hit + float(fact.confidence),
                channel=CHANNEL_FACT,
                text=fact.as_text(),
                metadata={
                    "subject": fact.subject_id,
                    "predicate": fact.predicate,
                    "object": fact.object_value,
                    "confidence": fact.confidence,
                    "status": fact.status,
                    "valid_from": fact.valid_from,
                    "valid_until": fact.valid_until,
                    "observers": list(fact.observers),
                },
                provenance=f"fact:{fact.predicate}",
            )
        )
    scored.sort(key=lambda doc: -doc.score)
    return scored[:limit]


def _provenance_weights(
    base: RerankWeights,
    *,
    importance: float = 0.0,
    confidence: float = 0.0,
) -> RerankWeights:
    """Weights that fold the extra query signals into the coverage term.

    The reranker only reorders - it never invents candidates - so this is the
    one place where the caller gets to say "this query is about an important
    event" without a second ranking pass.
    """
    return RerankWeights(
        pinned=base.pinned,
        focus=base.focus,
        exact=base.exact,
        coverage=base.coverage + max(0.0, confidence) * 0.6 + max(0.0, importance) * 0.6,
        channel_prior=base.channel_prior,
        depth_penalty=base.depth_penalty,
    )


def hybrid_rank(
    query: str,
    crystals: Sequence[CrystalDoc],
    *,
    engine_scores: Optional[Dict[str, float]] = None,
    facts: Sequence[FactDoc] = (),
    entity_index: Optional[EntityIndex] = None,
    pinned: Sequence[str] = (),
    focus: Sequence[str] = (),
    importance: float = 0.0,
    confidence: float = 0.0,
    limit: int = 24,
    fusion_config: Optional[FusionConfig] = None,
    weights: RerankWeights = DEFAULT_RERANK_WEIGHTS,
    vector: Optional[object] = None,
) -> HybridResult:
    """Run every channel, fuse with RRF, rerank, and report what happened.

    The caller applies its own permissions (who may see what) before calling
    this, and its token budget after: ranking never widens the audience.
    """
    index = entity_index or EntityIndex()
    result = HybridResult(query=str(query or ""), entities=mentioned_names(query, index))
    if not crystals and not facts:
        return result

    doc_ids = [doc.doc_id for doc in crystals]
    texts = [doc.text or "" for doc in crystals]
    metadata = [doc.as_meta() for doc in crystals]

    ranked_lists: List[Sequence[RetrievedDoc]] = []
    if texts and result.query.strip():
        try:
            ranked_lists.append(
                lexical_retriever.search(texts, result.query, doc_ids=doc_ids, limit=limit, metadata=metadata)
            )
        except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError) as exc:
            result.degraded.append(CHANNEL_BM25)
            result.errors.append(f"{CHANNEL_BM25}: {type(exc).__name__}: {exc}")
            logger.warning("bm25 channel failed for a memory query", exc_info=True)
    try:
        ranked_lists.append(_alias_channel(result.query, crystals, index, limit=limit))
    except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError) as exc:
        result.degraded.append(CHANNEL_ALIAS)
        result.errors.append(f"{CHANNEL_ALIAS}: {type(exc).__name__}: {exc}")
    ranked_lists.append(
        _engine_channel(crystals, engine_scores or {}, limit=limit)
    )
    try:
        ranked_lists.append(_fact_channel(result.query, facts, index, limit=limit))
    except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError) as exc:
        result.degraded.append(CHANNEL_FACT)
        result.errors.append(f"{CHANNEL_FACT}: {type(exc).__name__}: {exc}")
    if vector is not None:
        # Optional and last on purpose: the semantic channel is the only one
        # that can rank a crystal by meaning alone, so it may still add a
        # document no keyword channel found - but fusion keeps letting the
        # lexical and entity channels outvote a spurious cosine hit.
        try:
            ranked_lists.append(list(vector.search(result.query) or []))
        except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError) as exc:
            result.degraded.append(CHANNEL_VECTOR)
            result.errors.append(f"{CHANNEL_VECTOR}: {type(exc).__name__}: {exc}")
            logger.warning("vector channel failed for a memory query", exc_info=True)

    try:
        fused = reciprocal_rank_fusion(ranked_lists, fusion_config or FusionConfig())
    except (RuntimeError, ValueError, TypeError) as exc:
        result.degraded.append("fusion")
        result.errors.append(f"fusion: {type(exc).__name__}: {exc}")
        return result

    for ranked in ranked_lists:
        for doc in ranked:
            result.channels[doc.channel] = result.channels.get(doc.channel, 0) + 1

    signals = RerankSignals(
        pinned=frozenset(str(item) for item in pinned if str(item)),
        focus_tokens=frozenset(
            str(token).strip().casefold() for token in focus if str(token or "").strip()
        ),
    )
    result.documents = rerank(
        fused,
        result.query,
        signals,
        weights=_provenance_weights(weights, importance=importance, confidence=confidence),
        limit=limit,
    )
    return result


def crystal_docs_from_engine(crystals: Sequence) -> List[CrystalDoc]:
    """Flatten MemoryCrystal objects into retrieval documents.

    Consumed crystals are included on purpose: their prose is still evidence
    of what happened, and the temporal channel is what decides whether their
    content is still the current answer.
    """
    docs: List[CrystalDoc] = []
    for crystal in crystals or ():
        ai = getattr(crystal, "ai_content", "") or ""
        summary = getattr(crystal, "content", "") or ""
        text = ai if len(ai) >= len(summary) else summary
        if not text:
            continue
        tier = getattr(crystal, "tier", "")
        docs.append(
            CrystalDoc(
                doc_id=getattr(crystal, "memory_id", "") or f"mem_{id(crystal)}",
                text=text,
                tier=getattr(tier, "value", str(tier)),
                consumed=bool(getattr(crystal, "consumed", False)),
                entities=list(getattr(crystal, "related_entities", []) or []),
                witnesses=list(getattr(crystal, "witnessed_by", []) or []),
                confidence=float(getattr(crystal, "confidence", 1.0) or 0.0),
                importance=float(getattr(crystal, "importance", 0.0) or 0.0),
                start=str(getattr(crystal, "source_start_created_at", "") or ""),
                end=str(getattr(crystal, "source_end_created_at", "") or ""),
                always_include=bool(getattr(crystal, "always_include", False)),
            )
        )
    return docs


def fact_docs_from_store(fact_store, campaign_id: str, *, at: Optional[str] = None) -> List[FactDoc]:
    """Every readable fact, as retrieval documents. Empty when there is no store.

    `at` asks the store for facts valid at an instant, which is how a
    "what did she think of me in chapter two" query gets the enemy fact instead
    of the friend one.
    """
    if fact_store is None:
        return []
    try:
        facts = fact_store.find_facts(campaign_id, at=at, limit=500)
    except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError) as exc:
        logger.warning(
            "fact recall failed for campaign %s (%s); continuing without facts",
            campaign_id, exc,
        )
        return []
    docs: List[FactDoc] = []
    for fact in facts:
        docs.append(
            FactDoc(
                doc_id=getattr(fact, "id", ""),
                text="",
                subject_id=getattr(fact, "subject_id", "") or "",
                predicate=getattr(fact, "predicate", "") or "",
                object_value=getattr(fact, "object_value", "") or "",
                confidence=float(getattr(fact, "confidence", 0.5) or 0.0),
                status=str(getattr(fact, "status", "") or ""),
                valid_from=str(getattr(fact, "valid_from", "") or ""),
                valid_until=str(getattr(fact, "valid_until", "") or ""),
                observers=list(getattr(fact, "observer_ids", []) or []),
            )
        )
    return docs


def build_crystal_vector_channel(
    crystals: Sequence[CrystalDoc],
    *,
    builder=None,
):
    """Semantic channel for one set of crystals, or None when embeddings are off.

    `builder` defaults to app.services.retrieval_embedding.build_vector_channel,
    imported lazily so that importing this module never imports a model stack.
    A missing or broken embedding backend degrades to "no vector channel",
    never to a failed turn - the caller then runs on keyword and entity recall
    exactly as it did before this existed.
    """
    if not crystals:
        return None
    if builder is None:
        try:
            from app.services.retrieval_embedding import build_vector_channel

            builder = build_vector_channel
        except ImportError as exc:  # pragma: no cover - import-time only
            logger.warning("vector recall unavailable (%s)", exc)
            return None
    texts = [doc.text or "" for doc in crystals]
    doc_ids = [doc.doc_id for doc in crystals]
    metadata = [doc.as_meta() for doc in crystals]
    try:
        return builder(texts, doc_ids, metadata=metadata)
    except (ImportError, RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError) as exc:
        logger.warning("vector recall unavailable (%s); using keyword recall only", exc)
        return None


def build_entity_index_from_store(fact_store, campaign_id: str) -> EntityIndex:
    """Entity index for one campaign, or an empty index when there is no store."""
    if fact_store is None:
        return EntityIndex()
    try:
        return build_entity_index(fact_store.list_entities(campaign_id))
    except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError) as exc:
        logger.warning(
            "entity index unavailable for campaign %s (%s); alias recall disabled",
            campaign_id, exc,
        )
        return EntityIndex()
