"""Cross-layer reranking of the fused candidate list.

Fusion answers "who might be relevant"; the reranker answers "who is relevant *to
this turn*". It only ever reorders - it does not invent candidates - so a rerank
bug can make the order worse, never make the prompt show knowledge that the
filters excluded.

Signals, in the order the weights are applied:

* `pinned` - authored boost (a storyline node the player entered, an armed pack).
  This is the strongest statement an author can make, so it wins.
* `focus` - the entry is tagged with the current place, target or NPC.
* `exact` - a multi-character query phrase occurs verbatim in the document.
  BM25 tokenizes, so it can lose a phrase that spans a bigram boundary.
* `coverage` - how many distinct query terms occur; rewards a document that
  matches many weak signals over one that repeats a single strong one.
* `channel_prior` - a small constant per channel, so a document found by two
  channels outranks one found by a single channel at the same rank.
* `depth` / `layer` - the author's own ordering, used as the final tiebreak so
  the output is stable when nothing else separates two entries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from app.services.retrieval.models import RetrievedDoc
from app.services.retrieval.tokenizer import (
    DEFAULT_TOKENIZE_CONFIG,
    TokenizeConfig,
    normalize_query,
    token_set,
    tokenize_text,
)


@dataclass(frozen=True)
class RerankWeights:
    """Multipliers applied by :func:`rerank`; exposed for tuning and tests."""

    pinned: float = 3.0
    focus: float = 1.0
    exact: float = 0.8
    coverage: float = 0.6
    channel_prior: float = 0.1
    depth_penalty: float = 0.02


DEFAULT_RERANK_WEIGHTS = RerankWeights()


@dataclass(frozen=True)
class RerankSignals:
    """What the caller knows about a candidate that retrieval cannot infer.

    `pinned` is a set of doc ids; `focus_tokens` are already case-folded; and
    `depth`/`layer_rank` describe the author's intended order (lower is better).
    """

    pinned: frozenset = field(default_factory=frozenset)
    focus_tokens: frozenset = field(default_factory=frozenset)
    depth: Dict[str, int] = field(default_factory=dict)
    layer_rank: Dict[str, int] = field(default_factory=dict)


def rerank(
    candidates: Sequence[RetrievedDoc],
    query: str,
    signals: RerankSignals,
    weights: RerankWeights = DEFAULT_RERANK_WEIGHTS,
    tokenize_config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG,
    limit: Optional[int] = None,
) -> List[RetrievedDoc]:
    """Return `candidates` reordered by the signals above (never extended)."""
    if not candidates:
        return []
    query_tokens = tokenize_text(query, tokenize_config)
    query_set = set(query_tokens)
    normalized_query = normalize_query(query)
    # Only phrases of two or more characters/letters are worth an exact-match
    # bonus: a one-character match is already covered by the bigrams.
    phrases = [
        part for part in normalized_query.replace("\n", " ").split()
        if len(part) >= 2
    ]

    scored: List[tuple] = []
    for position, doc in enumerate(candidates):
        text = doc.text or ""
        score = doc.score
        provenance: List[str] = [doc.provenance] if doc.provenance else []

        if doc.doc_id in signals.pinned:
            score += weights.pinned
            provenance.append("pinned")

        if signals.focus_tokens:
            doc_tags = {
                str(tag).strip().casefold()
                for tag in (doc.metadata.get("tags") or [])
                if str(tag).strip()
            }
            hits = len(doc_tags & set(signals.focus_tokens))
            if hits:
                score += weights.focus * hits
                provenance.append(f"focus:{hits}")

        if phrases and text:
            lowered = normalize_query(text)
            hits = sum(1 for phrase in phrases if phrase in lowered)
            if hits:
                score += weights.exact * hits
                provenance.append(f"exact:{hits}")

        if query_set and text:
            covered = len(query_set & token_set(text, tokenize_config))
            if covered:
                score += weights.coverage * (covered / len(query_set))
                provenance.append(f"cover:{covered}")

        found_by = doc.metadata.get("fused_channels") or [doc.channel]
        if len(found_by) > 1:
            score += weights.channel_prior * (len(found_by) - 1)
            provenance.append(f"multi:{len(found_by)}")

        depth = signals.depth.get(doc.doc_id)
        if depth is not None:
            score -= weights.depth_penalty * depth

        layer = signals.layer_rank.get(doc.doc_id)
        scored.append(
            (
                -score,
                layer if layer is not None else 0,
                depth if depth is not None else 0,
                position,
                RetrievedDoc(
                    doc_id=doc.doc_id,
                    score=score,
                    channel=doc.channel,
                    text=text,
                    metadata=dict(doc.metadata),
                    provenance="|".join(p for p in provenance if p),
                ),
            )
        )

    scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    out = [item[4] for item in scored]
    return out[:limit] if limit is not None else out


def build_signals(
    pinned: Sequence[str] = (),
    focus: Sequence[str] = (),
    depth: Optional[Dict[str, int]] = None,
    layer_rank: Optional[Dict[str, int]] = None,
) -> RerankSignals:
    """Convenience constructor: normalises the two token sets."""
    return RerankSignals(
        pinned=frozenset(str(p) for p in pinned if str(p)),
        focus_tokens=frozenset(
            str(token).strip().casefold() for token in focus if str(token or "").strip()
        ),
        depth=dict(depth or {}),
        layer_rank=dict(layer_rank or {}),
    )
