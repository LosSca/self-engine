"""Process-wide wiring for the semantic channel.

`knowledge_service` must not import a model library, and the game layers must
not each own a provider: the point of an embedding cache is that it is shared.
This module is the single place that decides *whether* semantic recall is on,
and hands out a ready :class:`VectorRetriever`.

Three rules keep it honest:

* **Opt-in.** Without an embedding backend configured the factory returns
  `None`; callers then run lexically and report the channel as degraded. Nothing
  silently pretends semantics ran.
* **One provider per process.** The cache is keyed by (model fingerprint, text
  hash), so a shared provider cannot mix vector spaces.
* **Corpus reuse.** Building the same channel twice for the same document set
  returns the cached one; the knowledge entries of a scenario change only when
  an author edits them, so the second turn never re-embeds.

Index invalidation is explicit (:func:`rebuild`), because a silent rebuild on
every startup would make the first turn of each process pay a full embed.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Dict, List, Optional, Sequence, Tuple

from app.services.retrieval.embedding_index import EmbeddingCache
from app.services.retrieval.embedding_provider import (
    EmbeddingProvider,
    build_provider,
    config_from_env,
    probe,
)
from app.services.retrieval.vector_retriever import VectorRetriever

logger = logging.getLogger(__name__)

_PROVIDER: Optional[EmbeddingProvider] = None
_CACHE: Optional[EmbeddingCache] = None
_CHANNELS: Dict[Tuple[str, int], VectorRetriever] = {}
_LOCK = threading.Lock()


def cache_path() -> str:
    """Where the vector cache lives; the state directory, next to game data."""
    explicit = os.environ.get("ENGINE_EMBEDDING_CACHE", "").strip()
    if explicit:
        return explicit
    root = os.environ.get("ENGINE_STATE_DIR", "").strip()
    if root:
        return os.path.join(root, "embeddings.db")
    return os.path.join(os.getcwd(), "embeddings.db")


def enabled() -> bool:
    """True when this process has an embedding backend it can actually use."""
    return provider() is not None


def provider() -> Optional[EmbeddingProvider]:
    """The shared provider, built on first use; `None` when unavailable."""
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    with _LOCK:
        if _PROVIDER is None:
            built = build_provider(config_from_env())
            if built is None:
                return None
            _PROVIDER = built
    return _PROVIDER


def cache() -> Optional[EmbeddingCache]:
    """The shared vector cache; a cache failure is not fatal, just slower."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with _LOCK:
        if _CACHE is None:
            try:
                _CACHE = EmbeddingCache(cache_path())
            except (OSError, RuntimeError) as exc:
                logger.warning("embedding cache unavailable at %s: %s", cache_path(), exc)
                return None
    return _CACHE


def build_vector_channel(
    documents: Sequence[str],
    doc_ids: Sequence[str],
    metadata: Optional[Sequence[Dict[str, object]]] = None,
) -> Optional[VectorRetriever]:
    """A semantic channel for this corpus, or `None` when embeddings are off."""
    active = provider()
    if active is None or not documents:
        return None
    fingerprint = (active.fingerprint, len(documents))
    with _LOCK:
        existing = _CHANNELS.get(fingerprint)
        if existing is not None:
            return existing
    channel = VectorRetriever(
        active,
        list(documents),
        list(doc_ids),
        cache=cache(),
        metadata=list(metadata) if metadata else None,
    )
    with _LOCK:
        _CHANNELS[fingerprint] = channel
    return channel


def status() -> Dict[str, object]:
    """Health-panel view of the semantic channel."""
    described = probe(config_from_env())
    described["cache_path"] = cache_path()
    described["cached_vectors"] = cache().count() if _CACHE is not None else 0
    described["channels"] = len(_CHANNELS)
    return described


def rebuild() -> Dict[str, object]:
    """Drop cached vectors for the active model and forget built channels.

    Called after a model change (the fingerprint differs, so the old rows are
    dead weight) or when an operator wants a clean re-embed.
    """
    active = provider()
    dropped = 0
    if active is not None:
        store = cache()
        if store is not None:
            dropped = store.prune(active.fingerprint)
    with _LOCK:
        count = len(_CHANNELS)
        _CHANNELS.clear()
    return {"dropped_vectors": dropped, "forgotten_channels": count}


def reset_for_tests() -> None:
    """Clear the process-wide state; only the tests should need this."""
    global _PROVIDER, _CACHE
    with _LOCK:
        _CHANNELS.clear()
        if _CACHE is not None:
            _CACHE.close()
        _CACHE = None
        _PROVIDER = None


__all__ = [
    "build_vector_channel",
    "cache",
    "cache_path",
    "enabled",
    "provider",
    "rebuild",
    "reset_for_tests",
    "status",
]
