"""BM25 over an in-memory corpus.

Knowledge stores hold tens to hundreds of entries, crystals a few thousand, so
an inverted index built per call is cheaper than maintaining one on disk. The
scorer is Okapi BM25 with the usual `k1=1.5`, `b=0.75`, IDF floored at a small
positive number so that a term present in every document still contributes a
tie-breaking amount instead of a negative score (Lucene's convention).

Why BM25 and not the previous cosine: TF-IDF normalised by document length in a
way that punished long entries twice (once in the term count, once in the
normaliser), so a long encyclopedia entry could never outrank a short tag-like
one. BM25 saturates term frequency and only smooths length, which is what the
crystals and knowledge blocks actually need.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from app.services.retrieval.models import RetrievedDoc
from app.services.retrieval.tokenizer import TokenizeConfig, DEFAULT_TOKENIZE_CONFIG, tokenize_text


@dataclass(frozen=True)
class BM25Config:
    """Scoring knobs. Defaults follow Robertson/Sparck Jones and Lucene."""

    k1: float = 1.5
    b: float = 0.75
    idf_floor: float = 0.05


DEFAULT_BM25_CONFIG = BM25Config()


@dataclass
class _Index:
    """Term -> posting list (doc position, term frequency), plus length stats."""

    postings: Dict[str, Dict[int, int]] = field(default_factory=dict)
    doc_lengths: List[int] = field(default_factory=list)
    average_length: float = 0.0

    @property
    def size(self) -> int:
        return len(self.doc_lengths)


def build_index(documents: Sequence[str], config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG) -> _Index:
    """Tokenize `documents` and build the inverted index in one pass."""
    index = _Index()
    for position, text in enumerate(documents):
        tokens = tokenize_text(text, config)
        index.doc_lengths.append(len(tokens))
        for token in tokens:
            posting = index.postings.setdefault(token, {})
            posting[position] = posting.get(position, 0) + 1
    total = sum(index.doc_lengths)
    index.average_length = (total / len(index.doc_lengths)) if index.doc_lengths else 0.0
    return index


def _idf(index: _Index, token: str, floor: float) -> float:
    frequency = len(index.postings.get(token, {}))
    if frequency == 0 or index.size == 0:
        return 0.0
    # Lucene-style IDF: always positive, still monotone decreasing in frequency.
    value = math.log(1.0 + (index.size - frequency + 0.5) / (frequency + 0.5))
    return max(floor, value)


def score_query(
    index: _Index,
    query_tokens: Sequence[str],
    config: BM25Config = DEFAULT_BM25_CONFIG,
) -> List[float]:
    """BM25 score of every document for one query. Unknown terms are ignored."""
    scores = [0.0] * index.size
    if not query_tokens or index.size == 0:
        return scores
    average = index.average_length or 1.0
    # A repeated query term is a genuine relevance signal once, not once per
    # repetition: query term frequency is deliberately not applied.
    for token in dict.fromkeys(query_tokens):
        posting = index.postings.get(token)
        if not posting:
            continue
        idf = _idf(index, token, config.idf_floor)
        if idf <= 0.0:
            continue
        for position, term_frequency in posting.items():
            length = index.doc_lengths[position] or 1
            denominator = term_frequency + config.k1 * (
                1.0 - config.b + config.b * (length / average)
            )
            scores[position] += idf * (term_frequency * (config.k1 + 1.0)) / denominator
    return scores


def search(
    documents: Sequence[str],
    query: str,
    *,
    doc_ids: Optional[Sequence[str]] = None,
    limit: int = 10,
    channel: str = "bm25",
    config: BM25Config = DEFAULT_BM25_CONFIG,
    tokenize_config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG,
    metadata: Optional[Sequence[Dict[str, object]]] = None,
) -> List[RetrievedDoc]:
    """Top-`limit` documents for `query`, dropping documents that score zero.

    A zero score means "no query term occurs here"; returning those as filler
    would make every downstream fusion look healthy while injecting noise.
    """
    if not documents or not (query or "").strip():
        return []
    ids = list(doc_ids) if doc_ids is not None else [str(i) for i in range(len(documents))]
    index = build_index(documents, tokenize_config)
    query_tokens = tokenize_text(query, tokenize_config)
    scores = score_query(index, query_tokens, config)
    ranked = sorted(
        ((score, position) for position, score in enumerate(scores) if score > 0.0),
        key=lambda item: (-item[0], item[1]),
    )[: max(1, limit)]
    out: List[RetrievedDoc] = []
    for score, position in ranked:
        doc_metadata = dict(metadata[position]) if metadata and position < len(metadata) else {}
        out.append(
            RetrievedDoc(
                doc_id=ids[position],
                score=score,
                channel=channel,
                text=documents[position],
                metadata=doc_metadata,
                provenance=f"bm25:{len(query_tokens)}t",
            )
        )
    return out


def term_overlap(query_tokens: Iterable[str], document: str, config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG) -> int:
    """How many distinct query terms occur in `document` (used by rerankers)."""
    wanted = {token for token in query_tokens if token}
    if not wanted:
        return 0
    present = set(tokenize_text(document, config))
    return len(wanted & present)
