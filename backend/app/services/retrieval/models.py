"""Value types shared by every retrieval channel.

A retrieval channel (lexical, vector, graph) produces :class:`RetrievedDoc`
records; fusion turns several ranked lists into one; the reranker adjusts the
merged list. Keeping these types in one place is what lets a new channel be
added without touching the callers that consume knowledge or memory context.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


@dataclass(frozen=True)
class RetrievedDoc:
    """One candidate returned by a channel.

    `doc_id` is the identifier the owning layer understands (a knowledge entry
    id, a crystal event id, a graph node name). `score` is comparable only
    within the same channel and the same query: fusion normalises by rank.
    `provenance` records why the document was retrieved, which the evaluation
    harness and the debug panels report verbatim.
    """

    doc_id: str
    score: float
    channel: str
    text: str = ""
    metadata: Dict[str, object] = field(default_factory=dict)
    provenance: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "doc_id": self.doc_id,
            "score": self.score,
            "channel": self.channel,
            "text": self.text,
            "metadata": dict(self.metadata),
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class FusionConfig:
    """Knobs for reciprocal-rank fusion.

    `rrf_k` damps the influence of the very top of each list; the conventional
    value is 60 (Cormack et al., 2009). `weights` lets a deployment trust one
    channel more than another, and defaults to equal trust.
    """

    rrf_k: int = 60
    weights: Dict[str, float] = field(default_factory=dict)

    def weight_for(self, channel: str) -> float:
        try:
            return float(self.weights.get(channel, 1.0))
        except (TypeError, ValueError):
            return 1.0


@dataclass
class RetrievalTrace:
    """What every channel contributed, for debug panels and evaluation.

    The trace never feeds ranking decisions by itself: it is a record, so a
    reader can tell "BM25 found it but fusion dropped it" from "nothing found
    it" without re-running the query.
    """

    query: str
    channels: Dict[str, int] = field(default_factory=dict)
    fused: int = 0
    reranked: int = 0
    degraded: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "query": self.query,
            "channels": dict(self.channels),
            "fused": self.fused,
            "reranked": self.reranked,
            "degraded": list(self.degraded),
            "errors": list(self.errors),
        }


def channel_counts(results: Sequence[Sequence[RetrievedDoc]]) -> Dict[str, int]:
    """How many documents each channel returned, keyed by channel name."""
    counts: Dict[str, int] = {}
    for ranked in results:
        for doc in ranked:
            counts[doc.channel] = counts.get(doc.channel, 0) + 1
    return counts


def first_metadata(results: Sequence[RetrievedDoc], key: str, default: Optional[object] = None):
    """First non-empty `key` across `results`, used when a channel is optional."""
    for doc in results:
        value = doc.metadata.get(key)
        if value:
            return value
    return default
