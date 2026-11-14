"""Vector index: embedding cache plus nearest-neighbour search.

There is no separate vector service here on purpose. The corpora are small
(tens to thousands of documents per campaign), so an in-memory matrix beats a
network hop, and the expensive part - embedding - is cached in SQLite keyed by
`(fingerprint, sha1(text))`. Consequences worth stating:

* Creating a crystal embeds it once; later turns reuse the cached vector.
* Changing the model changes the fingerprint, so stale vectors are never
  compared with fresh ones: the index re-embeds instead.
* A missing cache row is not a failure; it is simply re-embedded.
* The scoring is exact cosine over normalised rows, so results are
  deterministic and comparable across runs - which the evaluation harness
  requires to report stable numbers.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from app.services.retrieval.embedding_provider import (
    EmbeddingProvider,
    EmbeddingUnavailable,
    normalise,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_cache (
    fingerprint TEXT NOT NULL,
    text_hash   TEXT NOT NULL,
    dimension   INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (fingerprint, text_hash)
);
"""


def _text_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _pack(vector: Sequence[float]) -> bytes:
    import array

    return array.array("f", [float(v) for v in vector]).tobytes()


def _unpack(blob: bytes) -> List[float]:
    import array

    values = array.array("f")
    values.frombytes(blob)
    return [float(v) for v in values]


class EmbeddingCache:
    """SQLite-backed cache of vectors, safe to share across threads."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    def get_many(self, fingerprint: str, texts: Sequence[str]) -> Dict[str, List[float]]:
        """Cached vectors for the texts that have one; missing keys are absent."""
        if not texts:
            return {}
        hashes = [_text_hash(text) for text in texts]
        found: Dict[str, List[float]] = {}
        with self._lock:
            connection = self._connection()
            for start in range(0, len(hashes), 200):
                chunk = hashes[start : start + 200]
                placeholders = ",".join("?" * len(chunk))
                rows = connection.execute(
                    f"SELECT text_hash, vector FROM embedding_cache "
                    f"WHERE fingerprint = ? AND text_hash IN ({placeholders})",
                    [fingerprint, *chunk],
                ).fetchall()
                for text_hash, blob in rows:
                    found[text_hash] = _unpack(blob)
        return found

    def put_many(self, fingerprint: str, items: Iterable[tuple]) -> None:
        """Store `(text, vector)` pairs; a write failure only costs a re-embed."""
        rows = [
            (fingerprint, _text_hash(text), len(vector), _pack(vector))
            for text, vector in items
            if vector
        ]
        if not rows:
            return
        with self._lock:
            connection = self._connection()
            try:
                connection.executemany(
                    "INSERT OR REPLACE INTO embedding_cache "
                    "(fingerprint, text_hash, dimension, vector) VALUES (?, ?, ?, ?)",
                    rows,
                )
                connection.commit()
            except sqlite3.Error:
                logger.warning("embedding cache write failed", exc_info=True)

    def count(self, fingerprint: str = "") -> int:
        with self._lock:
            connection = self._connection()
            if fingerprint:
                row = connection.execute(
                    "SELECT COUNT(*) FROM embedding_cache WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()
        return int(row[0]) if row else 0

    def prune(self, fingerprint: str) -> int:
        """Drop every row of a given fingerprint ("rebuild the index")."""
        with self._lock:
            connection = self._connection()
            cursor = connection.execute(
                "DELETE FROM embedding_cache WHERE fingerprint = ?", (fingerprint,)
            )
            connection.commit()
            return int(cursor.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


@dataclass
class EmbeddingIndex:
    """An in-memory matrix of normalised vectors over one document list.

    Positions are stable: `doc_ids[i]` owns `rows[i]`. That is what lets the
    vector channel report the same ids the lexical channel reports, so fusion
    can actually merge them.
    """

    doc_ids: List[str]
    rows: List[List[float]]
    provider_fingerprint: str
    embedded: int = 0
    cached: int = 0

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def dimension(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    def search(self, query_vector: Sequence[float], limit: int = 20) -> List[tuple]:
        """`(doc_id, cosine)` pairs, best first; zero-similarity rows are kept.

        Zero is kept (unlike BM25) because a vector dot product of exactly zero
        can still mean "orthogonal but on-topic"; the reranker and the budget
        decide whether it is worth sending. Ranking, not filtering, is the
        vector channel's job.
        """
        if not self.rows or not query_vector:
            return []
        query = normalise(query_vector)
        pairs: List[tuple] = []
        for position, row in enumerate(self.rows):
            if len(row) != len(query):
                continue
            score = sum(a * b for a, b in zip(row, query))
            pairs.append((self.doc_ids[position], score))
        pairs.sort(key=lambda item: (-item[1], item[0]))
        return pairs[: max(1, limit)]


def build_index(
    provider: EmbeddingProvider,
    documents: Sequence[str],
    doc_ids: Sequence[str],
    cache: Optional[EmbeddingCache] = None,
) -> EmbeddingIndex:
    """Embed (or reuse) every document and return a searchable matrix."""
    if len(documents) != len(doc_ids):
        raise ValueError("documents and doc_ids must have the same length")
    fingerprint = provider.fingerprint
    cached_vectors: Dict[str, List[float]] = {}
    if cache is not None:
        cached_vectors = cache.get_many(fingerprint, documents)

    pending: List[int] = []
    for position, text in enumerate(documents):
        if _text_hash(text) not in cached_vectors:
            pending.append(position)

    if pending:
        fresh = provider.embed([documents[i] for i in pending])
        if cache is not None:
            cache.put_many(
                fingerprint,
                ((documents[i], fresh[offset]) for offset, i in enumerate(pending)),
            )
        for offset, position in enumerate(pending):
            cached_vectors[_text_hash(documents[position])] = fresh[offset]

    rows = [cached_vectors[_text_hash(text)] for text in documents]
    return EmbeddingIndex(
        doc_ids=list(doc_ids),
        rows=rows,
        provider_fingerprint=fingerprint,
        embedded=len(pending),
        cached=len(documents) - len(pending),
    )


__all__ = ["EmbeddingCache", "EmbeddingIndex", "build_index", "EmbeddingUnavailable"]
