"""Retrieval evaluation: is the new stack actually better than the old one?

"We improved recall" is a claim, and a claim needs numbers. This module scores
a retriever against a labelled query set and reports the standard ranking
metrics, so two builds can be compared on the same data:

* **precision@k / recall@k** - how much of the returned shortlist is relevant,
  and how much of the relevant set was found;
* **MRR** - where the first relevant document appeared (what matters most for a
  prompt that can only carry a handful of entries);
* **nDCG@k** - the full ordering, discounted, so "relevant on top" beats
  "relevant somewhere";
* **hit@k** - did anything relevant appear at all.

The labels are (query, relevant doc ids) pairs. They can come from a hand-built
file or from a corpus's own tags; :func:`load_cases` reads a JSON file, and
:func:`tag_cases` turns "these entries are tagged with the query word" into
labels for a smoke check. A tag-derived set flatters a lexical retriever, so the
report always says where its labels came from.

Nothing here is on the request path: evaluation is a command line concern.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Sequence

from app.services.retrieval.models import RetrievedDoc


@dataclass(frozen=True)
class EvalCase:
    """One labelled query: what was asked, and what should come back."""

    query: str
    relevant: List[str]
    notes: str = ""
    corpus: str = "default"


@dataclass(frozen=True)
class CaseResult:
    """Per-query metrics, kept separate so a bad query is visible."""

    query: str
    retrieved: List[str]
    precision: float = 0.0
    recall: float = 0.0
    mrr: float = 0.0
    ndcg: float = 0.0
    hit: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "query": self.query,
            "retrieved": list(self.retrieved),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "mrr": round(self.mrr, 4),
            "ndcg": round(self.ndcg, 4),
            "hit": round(self.hit, 4),
        }


@dataclass
class EvalReport:
    """Aggregate metrics plus the per-query detail and label provenance."""

    name: str
    k: int
    label_source: str = ""
    results: List[CaseResult] = field(default_factory=list)

    @property
    def cases(self) -> int:
        return len(self.results)

    def mean(self, metric: str) -> float:
        if not self.results:
            return 0.0
        return sum(getattr(result, metric) for result in self.results) / len(self.results)

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "k": self.k,
            "cases": self.cases,
            "label_source": self.label_source,
            "precision": round(self.mean("precision"), 4),
            "recall": round(self.mean("recall"), 4),
            "mrr": round(self.mean("mrr"), 4),
            "ndcg": round(self.mean("ndcg"), 4),
            "hit": round(self.mean("hit"), 4),
            "detail": [result.as_dict() for result in self.results],
        }


def precision_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    if k <= 0:
        return 0.0
    top = list(retrieved)[:k]
    if not top:
        return 0.0
    wanted = set(relevant)
    return sum(1 for doc_id in top if doc_id in wanted) / len(top)


def recall_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    wanted = set(relevant)
    if not wanted:
        return 0.0
    top = set(list(retrieved)[: max(0, k)])
    return len(top & wanted) / len(wanted)


def reciprocal_rank(retrieved: Sequence[str], relevant: Sequence[str]) -> float:
    wanted = set(relevant)
    for rank, doc_id in enumerate(retrieved, start=1):
        if doc_id in wanted:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(retrieved: Sequence[str], relevant: Sequence[str], k: int) -> float:
    """Binary-gain nDCG: discount by log2(rank + 1), normalise by the ideal."""
    wanted = set(relevant)
    if not wanted or k <= 0:
        return 0.0
    gained = 0.0
    for rank, doc_id in enumerate(list(retrieved)[:k], start=1):
        if doc_id in wanted:
            gained += 1.0 / math.log2(rank + 1)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(wanted), k) + 1))
    return gained / ideal if ideal else 0.0


def evaluate(
    name: str,
    cases: Sequence[EvalCase],
    retrieve: Callable[[EvalCase], Sequence[RetrievedDoc]],
    k: int = 5,
    label_source: str = "",
) -> EvalReport:
    """Run every case through `retrieve` and score the ranking."""
    report = EvalReport(name=name, k=k, label_source=label_source)
    for case in cases:
        try:
            ranked = [doc.doc_id for doc in retrieve(case) or []]
        except (ValueError, TypeError, KeyError, RuntimeError, OSError):
            # One broken query must not abort a comparison run; it scores zero
            # and stays visible in the detail list.
            ranked = []
        report.results.append(
            CaseResult(
                query=case.query,
                retrieved=ranked,
                precision=precision_at_k(ranked, case.relevant, k),
                recall=recall_at_k(ranked, case.relevant, k),
                mrr=reciprocal_rank(ranked, case.relevant),
                ndcg=ndcg_at_k(ranked, case.relevant, k),
                hit=1.0 if set(ranked[:k]) & set(case.relevant) else 0.0,
            )
        )
    return report


def load_cases(path: str) -> List[EvalCase]:
    """Read a JSON label file: `[{"query": ..., "relevant": [...]}, ...]`."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list):
        raise ValueError("label file must contain a JSON array")
    cases: List[EvalCase] = []
    for item in raw:
        if not isinstance(item, dict) or "query" not in item:
            continue
        relevant = item.get("relevant") or item.get("ids") or []
        cases.append(
            EvalCase(
                query=str(item["query"]),
                relevant=[str(doc_id) for doc_id in relevant],
                notes=str(item.get("notes", "")),
                corpus=str(item.get("corpus", "default")),
            )
        )
    return cases


def tag_cases(entries: Sequence, corpus: str = "default") -> List[EvalCase]:
    """Smoke labels derived from entry tags.

    Each tag becomes a query whose relevant set is the entries carrying it. This
    is circular for a lexical retriever (the tag is also indexed as a term), so
    it is a *smoke check* that the pipeline is wired, not evidence of quality.
    """
    by_tag: Dict[str, List[str]] = {}
    for entry in entries:
        entry_id = str(getattr(entry, "id", ""))
        for tag in getattr(entry, "tags", []) or []:
            cleaned = str(tag or "").strip()
            if cleaned:
                by_tag.setdefault(cleaned, []).append(entry_id)
    return [
        EvalCase(query=tag, relevant=ids, corpus=corpus)
        for tag, ids in sorted(by_tag.items())
        if ids
    ]


def compare(reports: Sequence[EvalReport], metric: str = "mrr") -> List[Dict[str, object]]:
    """Side-by-side rows for a report table, best first on `metric`."""
    rows = [
        {
            "name": report.name,
            "cases": report.cases,
            "precision": round(report.mean("precision"), 4),
            "recall": round(report.mean("recall"), 4),
            "mrr": round(report.mean("mrr"), 4),
            "ndcg": round(report.mean("ndcg"), 4),
            "hit": round(report.mean("hit"), 4),
        }
        for report in reports
    ]
    rows.sort(key=lambda row: -float(row.get(metric, 0.0)))
    return rows


__all__ = [
    "EvalCase",
    "EvalReport",
    "CaseResult",
    "compare",
    "evaluate",
    "load_cases",
    "ndcg_at_k",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "tag_cases",
]
