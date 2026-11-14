"""The retrieval pipeline the game layers call.

Deterministic filters (run by the caller) decide what is *allowed*; the
channels decide what is *relevant*; fusion and reranking decide the order::

    filters -> BM25 -> [vector] -> [graph]
                 +--------+-----------+--> RRF -> rerank -> result

Every optional stage is best-effort: a channel that raises is recorded in the
trace and skipped, and the pipeline still returns the documents the remaining
channels found. That is deliberate - knowledge recall is advisory, and a
missing optional dependency must not turn into a failed turn. What the old code
did wrong was *silence*: `except Exception: pass` around the whole knowledge
block hid real bugs. Here the failure is visible in :class:`RetrievalTrace`
and in `degraded`, so a panel can show it.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from app.services.retrieval import lexical_retriever
from app.services.retrieval.fusion import reciprocal_rank_fusion
from app.services.retrieval.models import (
    FusionConfig,
    RetrievalTrace,
    RetrievedDoc,
)
from app.services.retrieval.query_builder import QueryParts
from app.services.retrieval.vector_retriever import VectorRetriever
from app.services.retrieval.reranker import (
    DEFAULT_RERANK_WEIGHTS,
    RerankSignals,
    RerankWeights,
    rerank,
)

logger = logging.getLogger(__name__)

#: What a channel raised, named so the trace stays JSON-friendly.
_CHANNEL_ERRORS = (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError, ImportError)


class RetrievalService:
    """Runs the channels over one corpus and returns a fused, reranked list."""

    def __init__(
        self,
        *,
        fusion_config: Optional[FusionConfig] = None,
        rerank_weights: RerankWeights = DEFAULT_RERANK_WEIGHTS,
        lexical_limit: int = 50,
        final_limit: int = 24,
    ):
        self._fusion_config = fusion_config or FusionConfig()
        self._rerank_weights = rerank_weights
        self._lexical_limit = lexical_limit
        self._final_limit = final_limit

    def retrieve(
        self,
        documents: Sequence[str],
        doc_ids: Sequence[str],
        query: QueryParts,
        *,
        metadata: Optional[Sequence[Dict[str, object]]] = None,
        signals: Optional[RerankSignals] = None,
        vector: Optional[VectorRetriever] = None,
        graph: Optional["GraphChannel"] = None,
        final_limit: Optional[int] = None,
    ) -> tuple[List[RetrievedDoc], RetrievalTrace]:
        """Documents for one query, best first, plus what each channel did."""
        query_text = query.text()
        trace = RetrievalTrace(query=query_text)
        if not documents or not query_text:
            return [], trace

        ranked_lists: List[Sequence[RetrievedDoc]] = []

        lexical = self._run_channel(
            "bm25",
            lambda: lexical_retriever.search(
                documents,
                query_text,
                doc_ids=doc_ids,
                limit=self._lexical_limit,
                metadata=metadata,
            ),
            trace,
        )
        ranked_lists.append(lexical)

        if vector is not None:
            ranked_lists.append(self._run_channel("vector", lambda: vector.search(query_text), trace))

        if graph is not None:
            ranked_lists.append(self._run_channel("graph", lambda: list(graph.search(query_text)), trace))

        fused = reciprocal_rank_fusion(ranked_lists, self._fusion_config)
        #: Documents no channel found lexically still deserve the semantic
        #: channel, but a vector-only tail must not crowd out the rerank
        #: window: fusion already ordered by rank, and the corpora are small
        #: enough that the caller's budget (not this truncation) decides.
        trace.fused = len(fused)
        reranked = rerank(
            fused,
            query_text,
            signals or RerankSignals(),
            weights=self._rerank_weights,
            limit=final_limit if final_limit is not None else self._final_limit,
        )
        trace.reranked = len(reranked)
        for ranked in ranked_lists:
            for doc in ranked:
                trace.channels[doc.channel] = trace.channels.get(doc.channel, 0) + 1
        return reranked, trace

    def _run_channel(self, name: str, run, trace: RetrievalTrace) -> List[RetrievedDoc]:
        """Run one channel; a failure degrades to an empty list, and is recorded."""
        try:
            return list(run() or [])
        except _CHANNEL_ERRORS as exc:
            trace.degraded.append(name)
            trace.errors.append(f"{name}: {type(exc).__name__}: {exc}"[:200])
            logger.warning("retrieval channel %s failed", name, exc_info=True)
            return []


__all__ = ["RetrievalService", "RetrievedDoc", "RetrievalTrace"]


# The vector and graph channels are typed through structural protocols below so
# that this module never imports an optional dependency at import time.
from typing import Protocol, runtime_checkable  # noqa: E402


@runtime_checkable
class VectorChannel(Protocol):  # pragma: no cover - structural type only
    """Anything that can turn a query into ranked documents by meaning."""

    def search(self, query: str) -> Sequence[RetrievedDoc]:
        ...


@runtime_checkable
class GraphChannel(Protocol):  # pragma: no cover - structural type only
    """Anything that can turn a query into ranked relationship facts."""

    def search(self, query: str) -> Sequence[RetrievedDoc]:
        ...
