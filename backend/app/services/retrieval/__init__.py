"""Retrieval infrastructure shared by knowledge, memory and graph recall.

The package is deliberately layered so each stage can be replaced alone:

1. deterministic filters (scope, depth, audience, budget) live in the caller;
2. lexical recall (BM25) in :mod:`lexical_retriever`;
3. vector recall in :mod:`vector_retriever` (optional embedding backend);
4. graph recall in :mod:`retrieval_service` (optional Graphiti/Neo4j);
5. reciprocal-rank fusion in :mod:`fusion`, then cross-layer reranking in
   :mod:`reranker`.

Nothing here writes game state: a retrieval channel that fails degrades to the
other channels and records the failure in :class:`models.RetrievalTrace`.
"""

from app.services.retrieval.models import (
    FusionConfig,
    RetrievalTrace,
    RetrievedDoc,
)

__all__ = ["FusionConfig", "RetrievalTrace", "RetrievedDoc"]
