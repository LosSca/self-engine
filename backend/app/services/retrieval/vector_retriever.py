"""Vector channel: ranking documents by meaning.

The channel is a thin, failure-tolerant wrapper: it owns no model, no cache and
no policy beyond "embeddings that are unavailable mean no results". That keeps
it swappable - the evaluation harness can compare "BM25 only" with "BM25 +
this channel" without changing anything here.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from app.services.retrieval.embedding_index import EmbeddingCache, build_index
from app.services.retrieval.embedding_provider import (
    EmbeddingProvider,
    EmbeddingUnavailable,
    normalise,
)
from app.services.retrieval.models import RetrievedDoc

logger = logging.getLogger(__name__)

CHANNEL = "vector"


class VectorRetriever:
    """Searches one corpus for a query, by embedding similarity.

    A retriever is cheap to build and is meant to be cached by the caller when
    the corpus does not change between turns (knowledge entries change only
    when the author edits them; crystals change every few turns).
    """

    def __init__(
        self,
        provider: EmbeddingProvider,
        documents: Sequence[str],
        doc_ids: Sequence[str],
        *,
        cache: Optional[EmbeddingCache] = None,
        metadata: Optional[Sequence[dict]] = None,
        limit: int = 30,
    ):
        self._provider = provider
        self._documents = list(documents)
        self._doc_ids = list(doc_ids)
        self._metadata = list(metadata) if metadata else None
        self._limit = limit
        self._index = None
        self._error = ""
        self._cache = cache
        #: doc id -> position, built once: the search path used to call
        #: `list.index` per hit, which is quadratic on a large corpus.
        self._positions = {
            doc_id: position for position, doc_id in enumerate(self._doc_ids)
        }

    @property
    def available(self) -> bool:
        return self._provider is not None and bool(self._documents)

    @property
    def error(self) -> str:
        return self._error

    def _ensure_index(self):
        if self._index is not None:
            return self._index
        if self._provider is None:
            raise EmbeddingUnavailable("no embedding provider")
        self._index = build_index(
            self._provider,
            self._documents,
            self._doc_ids,
            cache=self._cache,
        )
        return self._index

    def search(self, query: str) -> List[RetrievedDoc]:
        """Top matches for one query; an unavailable backend yields []."""
        text = (query or "").strip()
        if not text or not self.available:
            return []
        try:
            index = self._ensure_index()
            query_vector = normalise(self._provider.embed([text])[0])
        except EmbeddingUnavailable as exc:
            self._error = str(exc)
            logger.info("vector channel unavailable: %s", exc)
            return []
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("vector channel failed", exc_info=True)
            return []

        out: List[RetrievedDoc] = []
        for doc_id, score in index.search(query_vector, limit=self._limit):
            position = self._positions.get(doc_id, -1)
            if position < 0:
                continue
            metadata = (
                dict(self._metadata[position])
                if self._metadata and position < len(self._metadata)
                else {}
            )
            out.append(
                RetrievedDoc(
                    doc_id=doc_id,
                    score=float(score),
                    channel=CHANNEL,
                    text=self._documents[position],
                    metadata=metadata,
                    provenance=f"cos:{index.provider_fingerprint}",
                )
            )
        return out


__all__ = ["VectorRetriever", "CHANNEL"]
