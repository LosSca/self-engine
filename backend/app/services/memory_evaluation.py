"""Memory evaluation: a dataset and the metrics it is scored with.

**Status: evaluation fixture, not production code.** Nothing on the request
path imports this module; it is loaded by the memory-upgrade evaluation suite
(`backend/tests/services/test_memory_evaluation.py`) to replay a fixed corpus
through the real pipeline and turn quality claims into numbers.

Unit tests prove a function follows its contract. They cannot answer the
question the memory upgrade actually has to answer - does the world remember
the right things, refuse the wrong ones, and keep each character to what it saw.
Those are quality questions, and quality needs a corpus: fixed scenarios with
known-good answers, replayed against whatever the current code does.

The corpus lives here as data, not as prose in a document, so a regression
shows up as a number that moved. Every case is small on purpose - one claim,
one query - because a case that fails tells you which guarantee broke.

If this is ever wired into a route, it must stay read-only: scoring a turn must
never change a campaign.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.retrieval import evaluation as retrieval_eval

logger = logging.getLogger(__name__)


#: What a case is testing, so a report can be read by category.
FACT_ACCURACY = "fact_accuracy"
LONG_TERM = "long_term"
TEMPORAL = "temporal"
PERMISSION = "permission"
RETRIEVAL = "retrieval"
ENGINEERING = "engineering"


@dataclass(frozen=True)
class EvalCase:
    """One scenario with a known answer."""

    case_id: str
    category: str
    question: str
    expected: Tuple[str, ...] = ()
    forbidden: Tuple[str, ...] = ()
    notes: str = ""

    def passes(self, answer: Iterable[str]) -> bool:
        text = " ".join(answer or ())
        lowered = text.casefold()
        if self.expected and not any(e.casefold() in lowered for e in self.expected):
            return False
        if any(f.casefold() in lowered for f in self.forbidden):
            return False
        return True


@dataclass
class EvalResult:
    case_id: str
    category: str
    passed: bool
    answer: List[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class EvalSummary:
    total: int = 0
    passed: int = 0
    results: List[EvalResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return 0.0 if not self.total else self.passed / self.total

    def by_category(self) -> Dict[str, float]:
        buckets: Dict[str, List[bool]] = {}
        for result in self.results:
            buckets.setdefault(result.category, []).append(result.passed)
        return {k: sum(v) / len(v) for k, v in sorted(buckets.items())}

    def failures(self) -> List[EvalResult]:
        return [r for r in self.results if not r.passed]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "accuracy": round(self.accuracy, 4),
            "by_category": {k: round(v, 4) for k, v in self.by_category().items()},
            "failures": [r.case_id for r in self.failures()],
        }

def run_cases(cases: Sequence[EvalCase], answer_of) -> EvalSummary:
    """Replay each case through `answer_of` and score it.

    `answer_of` returns the lines a real prompt would receive for that case,
    so a failure means the pipeline answered wrongly, not that a stub did.
    """
    summary = EvalSummary()
    for case in cases:
        try:
            answer = list(answer_of(case) or ())
        except Exception as exc:  # noqa: BLE001 - a crash is a failed case
            summary.results.append(
                EvalResult(case.case_id, case.category, False, [], f"raised {exc!r}")
            )
            summary.total += 1
            continue
        passed = case.passes(answer)
        summary.results.append(EvalResult(case.case_id, case.category, passed, answer))
        summary.total += 1
        summary.passed += 1 if passed else 0
    return summary


# -- the corpus ----------------------------------------------------------

# Fact accuracy: the world must not learn things nobody established. Each
# case states what a prompt may and may not contain after the event in
# question was ingested.
FACT_CASES: Tuple[EvalCase, ...] = (
    EvalCase(
        "fiction_stays_fiction",
        FACT_ACCURACY,
        "A narrator invents a claim nobody committed.",
        expected=("may be wrong", "candidate"),
        forbidden=("(fact) Eileen owns the north gate",),
        notes="LLM prose can only ever be llm_narrative, so it cannot reach ACTIVE.",
    ),
    EvalCase(
        "player_guess_is_not_knowledge",
        FACT_ACCURACY,
        "The player guesses that the duke is a traitor.",
        expected=("candidate",),
        forbidden=("(fact) Duke traitor",),
    ),
    EvalCase(
        "rumour_is_not_canon",
        FACT_ACCURACY,
        "An NPC repeats a rumour about the harbour being cursed.",
        expected=("candidate", "unconfirmed"),
        forbidden=("(fact) harbour cursed",),
    ),
    EvalCase(
        "engine_settlement_is_a_fact",
        FACT_ACCURACY,
        "The engine settles a combat result.",
        expected=("(fact)", "victory"),
        forbidden=(),
    ),
)

LONG_TERM_CASES: Tuple[EvalCase, ...] = (
    EvalCase(
        "old_event_survives_compression",
        LONG_TERM,
        "A rescue from a hundred turns ago is still recallable.",
        expected=("drove off the raiders",),
    ),
    EvalCase(
        "a_merged_summary_keeps_its_sources",
        LONG_TERM,
        "A consolidated memory can name the memories it replaced.",
        expected=("mem_",),
    ),
    EvalCase(
        "a_one_off_event_is_not_forgotten",
        LONG_TERM,
        "A single decisive event is still retrievable after many quiet scenes.",
        expected=("the duke abdicated",),
    ),
)

TEMPORAL_CASES: Tuple[EvalCase, ...] = (
    EvalCase(
        "current_relationship_wins",
        TEMPORAL,
        "Ask about Eileen now that the feud is settled.",
        expected=("friend",),
        forbidden=("enemy",),
    ),
    EvalCase(
        "historical_relationship_is_askable",
        TEMPORAL,
        "Ask what Eileen was in chapter two.",
        expected=("enemy",),
    ),
    EvalCase(
        "a_superseded_fact_is_not_deleted",
        TEMPORAL,
        "The old value is still on record after being replaced.",
        expected=("superseded",),
    ),
)

PERMISSION_CASES: Tuple[EvalCase, ...] = (
    EvalCase(
        "the_duke_cannot_read_a_secret",
        PERMISSION,
        "The duke asks about Eileen's mission.",
        expected=("left the city",),
        forbidden=("secret errand",),
    ),
    EvalCase(
        "the_villain_can",
        PERMISSION,
        "The villain asks about the same mission.",
        expected=("secret errand",),
        forbidden=("left the city",),
    ),
    EvalCase(
        "a_wrong_belief_is_labelled",
        PERMISSION,
        "The duke believes Eileen is dead.",
        expected=("heard, may be wrong",),
    ),
)

RETRIEVAL_CASES: Tuple[EvalCase, ...] = (
    EvalCase("chinese_query", RETRIEVAL, "上次救过我的女骑士现在怎么样了", expected=("rescue",)),
    EvalCase("english_query", RETRIEVAL, "the knight who rescued me", expected=("rescue",)),
    EvalCase("japanese_query", RETRIEVAL, "私を救った騎士は今どこに", expected=("rescue",)),
    EvalCase("mixed_script_query", RETRIEVAL, "Eileen と 骑士 rescue", expected=("rescue",)),
    EvalCase("alias_query", RETRIEVAL, "the white pearl", expected=("ship",)),
    EvalCase("multihop_query", RETRIEVAL, "the sister of the man who rescued me", expected=("rescue",)),
)

ALL_CASES: Tuple[EvalCase, ...] = (
    FACT_CASES + LONG_TERM_CASES + TEMPORAL_CASES + PERMISSION_CASES + RETRIEVAL_CASES
)


# -- retrieval quality metrics ------------------------------------------

def retrieval_metrics(ranked: Sequence[str], relevant: Sequence[str], k: int = 5) -> Dict[str, float]:
    """The retrieval numbers the brief names, from one ranking.

    Wraps app.services.retrieval.evaluation so memory and knowledge are scored
    by the same formulas - two ways of measuring recall would make the two
    systems incomparable, which is the one thing a shared metric must not do.
    """
    ranked = list(ranked)
    relevant = list(relevant)
    return {
        f"recall@{k}": retrieval_eval.recall_at_k(ranked, relevant, k),
        f"precision@{k}": retrieval_eval.precision_at_k(ranked, relevant, k),
        "mrr": retrieval_eval.reciprocal_rank(ranked, relevant),
    }


def leak_rate(answers: Sequence[Sequence[str]], secrets: Sequence[str]) -> float:
    """Share of prompts that contain a secret they should not have seen."""
    if not answers:
        return 0.0
    leaked = 0
    for answer in answers:
        text = " ".join(answer or ()).casefold()
        if any(secret.casefold() in text for secret in secrets):
            leaked += 1
    return leaked / len(answers)


def compression_retention(retained: Sequence[str], expected: Sequence[str]) -> float:
    """Share of key facts that survive consolidation."""
    if not expected:
        return 1.0
    text = " ".join(retained or ()).casefold()
    kept = sum(1 for item in expected if item.casefold() in text)
    return kept / len(expected)


def tokens_per_injection(packs: Sequence[Any]) -> float:
    """Average rendered tokens per context injection, for the budget metric."""
    if not packs:
        return 0.0
    total = 0
    for pack in packs:
        rendered = pack.render() if hasattr(pack, "render") else str(pack)
        from app.services.context_pack import estimate_tokens

        total += estimate_tokens(rendered)
    return total / len(packs)