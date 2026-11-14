"""Reciprocal-rank fusion (RRF).

Each channel produces a ranked list on its own scale: BM25 scores are unbounded,
cosine similarities live in `[-1, 1]`, and a graph channel returns hop distance
turned into a score. Adding those numbers would let whichever scale is widest
decide the outcome. RRF only uses the *rank* (`1 / (k + rank)`), so a channel
that is merely confident in its own units cannot drown the others.

`weights` expresses trust, not score: the graph channel may be weighted higher
because it only speaks when it recognises an entity by name.
"""
from __future__ import annotations

from typing import Dict, List, Sequence

from app.services.retrieval.models import FusionConfig, RetrievedDoc


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[RetrievedDoc]],
    config: FusionConfig = FusionConfig(),
) -> List[RetrievedDoc]:
    """Merge ranked lists into one, keeping the first document seen per id.

    Ties break on the best single-channel rank, then on doc id, so the result
    is deterministic for a given input - which the evaluation harness relies on.
    """
    best_rank: Dict[str, int] = {}
    fused: Dict[str, float] = {}
    keep: Dict[str, RetrievedDoc] = {}
    channels: Dict[str, List[str]] = {}

    for ranked in ranked_lists:
        for rank, doc in enumerate(ranked, start=1):
            if not doc.doc_id:
                continue
            weight = config.weight_for(doc.channel)
            fused[doc.doc_id] = fused.get(doc.doc_id, 0.0) + weight / (config.rrf_k + rank)
            best_rank[doc.doc_id] = min(best_rank.get(doc.doc_id, rank), rank)
            channels.setdefault(doc.doc_id, []).append(doc.channel)
            existing = keep.get(doc.doc_id)
            # Keep the richest text/metadata: a BM25 hit carries the document
            # body, while a graph hit may only carry a fact summary.
            if existing is None or (len(doc.text) > len(existing.text)):
                keep[doc.doc_id] = doc

    out: List[RetrievedDoc] = []
    for doc_id, score in fused.items():
        doc = keep[doc_id]
        provenance = "+".join(dict.fromkeys(channels.get(doc_id, [])))
        out.append(
            RetrievedDoc(
                doc_id=doc_id,
                score=score,
                channel=doc.channel,
                text=doc.text,
                metadata={**doc.metadata, "fused_channels": list(dict.fromkeys(channels.get(doc_id, [])))},
                provenance=f"rrf[{provenance}]:{best_rank[doc_id]}",
            )
        )
    out.sort(key=lambda d: (-d.score, best_rank.get(d.doc_id, 0), d.doc_id))
    return out
