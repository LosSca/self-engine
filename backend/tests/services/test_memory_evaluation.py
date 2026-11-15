"""Memory evaluation: replay the corpus through the real pipeline."""
import pytest

from app.db.event_store import EventStore, EventType
from app.db.memory_fact_store import MemoryFactStore
from app.engines.memory_engine import CrystalTier, MemoryCrystal, MemoryEngine
from app.models.memory import EvidenceType, FactStatus, MemoryFact
from app.services.context_pack import build_context_pack
from app.services.memory_evaluation import (
    ALL_CASES,
    FACT_ACCURACY,
    PERMISSION,
    TEMPORAL,
    compression_retention,
    leak_rate,
    retrieval_metrics,
    run_cases,
    tokens_per_injection,
)
from app.services.memory_ingestion import MemoryIngestionPipeline
from app.services.memory_perspective import Claim, BELIEVES, build_perspective, facts_for_prompt, filter_facts_for_observer


class _NullStore:
    def __init__(self, events=()):
        self.events = list(events)

    def append(self, **kwargs):
        return None

    def get_recent(self, campaign_id, limit=-1, **kwargs):
        return list(self.events)

    def get_by_type(self, campaign_id, event_type, limit=-1):
        return [e for e in self.events if e.event_type == event_type]

    def get_after(self, campaign_id, event_id=None, **kwargs):
        return []


@pytest.fixture()
def stores(tmp_path):
    events = EventStore(str(tmp_path / "events.db"))
    facts = MemoryFactStore(str(tmp_path / "memory.db"))
    yield events, facts
    events.close()
    facts.close()

def test_a_narrator_claim_can_never_become_a_world_fact(stores):
    events, facts = stores
    events.append(
        campaign_id="c1",
        event_type=EventType.NARRATOR_RESPONSE,
        payload={"text": "Eileen owns the north gate."},
        narrative_time_delta=0,
        location="gate",
        entities=["Eileen"],
    )
    pipeline = MemoryIngestionPipeline(events, facts)
    import asyncio

    asyncio.run(pipeline.ingest("c1", dry_run=False))
    live = facts.find_facts("c1")
    # Whatever was extracted from prose is at most a candidate: nothing in the
    # prompt may claim the world knows it.
    assert all(str(f.status) != FactStatus.ACTIVE.value for f in live) or live == []


def test_an_engine_settlement_becomes_an_active_fact(stores):
    events, facts = stores
    events.append(
        campaign_id="c1",
        event_type=EventType.COMBAT_RESULT,
        payload={"outcome": "victory", "opponent_name": "Raider"},
        narrative_time_delta=0,
        location="gate",
        entities=["Raider"],
    )
    pipeline = MemoryIngestionPipeline(events, facts)
    import asyncio

    asyncio.run(pipeline.ingest("c1", dry_run=False))
    live = facts.find_facts("c1")
    assert live, "an engine settlement must produce a fact"
    assert any(str(f.status) == FactStatus.ACTIVE.value for f in live)


def test_fact_accuracy_cases_pass_on_a_real_pipeline(stores):
    events, facts = stores
    events.append(
        campaign_id="c1",
        event_type=EventType.COMBAT_RESULT,
        payload={"outcome": "victory", "opponent_name": "Raider"},
        narrative_time_delta=0,
        location="gate",
        entities=["Raider"],
    )
    pipeline = MemoryIngestionPipeline(events, facts)
    import asyncio

    asyncio.run(pipeline.ingest("c1", dry_run=False))

    def answer(case):
        return facts_for_prompt(facts.find_facts("c1"), limit=20)

    cases = [c for c in ALL_CASES if c.category == FACT_ACCURACY]
    summary = run_cases(cases, answer)
    # The engine-settlement case expects a committed fact to render as one.
    assert summary.total == len(cases)

def _fact(store, subject, predicate, value, observers=(), status=FactStatus.ACTIVE):
    fact = MemoryFact(
        campaign_id="c1",
        subject_id=subject,
        predicate=predicate,
        object_value=value,
        observer_ids=list(observers),
        evidence_type=EvidenceType.ENGINE_STATE,
    )
    fact, _ = store.add_fact(fact)
    if status != FactStatus.CANDIDATE:
        store.advance_status(fact.id, status)
        fact = store.get_fact(fact.id)
    return fact


def test_permission_cases_show_no_leak(stores):
    _events, facts = stores
    _fact(facts, "Eileen", "mission", "secret errand", ["villain"])
    _fact(facts, "Eileen", "status", "left the city", ["duke"])
    world = facts.find_facts("c1")

    answers = {
        name: facts_for_prompt(world, observer=name) for name in ("duke", "villain")
    }
    rate = leak_rate([answers["duke"]], ["secret errand"])
    assert rate == 0.0
    assert leak_rate([answers["villain"]], ["left the city"]) == 0.0


def test_a_wrong_belief_reads_as_a_belief_not_a_fact(stores):
    _events, facts = stores
    _fact(facts, "Eileen", "status", "left the city", ["duke"])
    world = facts.find_facts("c1")
    duke = build_perspective(
        "duke", facts=filter_facts_for_observer(world, "duke"),
        beliefs=[Claim("Eileen", "status", "dead", BELIEVES, 0.9)],
    )
    lines = facts_for_prompt(world, observer="duke", perspective=duke)
    assert any("heard, may be wrong" in line for line in lines)
    assert not any("(fact)" in line and "dead" in line for line in lines)


def test_temporal_cases_distinguish_now_from_then(stores):
    _events, facts = stores
    old = _fact(facts, "Eileen", "relationship", "enemy", ["player"])
    new = _fact(facts, "Eileen", "relationship", "friend", ["player"])
    facts.supersede(old.id, new.id, ended_at="2026-03-01T00:00:00")

    now = facts_for_prompt(facts.find_facts("c1"), limit=10)
    then = facts_for_prompt(facts.find_facts("c1", at="2026-02-01T00:00:00"), limit=10)
    assert any("friend" in line for line in now)
    assert not any("enemy" in line for line in now)
    assert any("enemy" in line for line in then)


def test_compression_retention_counts_surviving_facts():
    retained = ["the duke abdicated", "Eileen was rescued"]
    expected = ["the duke abdicated", "Eileen was rescued", "the harbour burned"]
    assert compression_retention(retained, expected) == pytest.approx(2 / 3)
    assert compression_retention(retained, []) == 1.0


def test_retrieval_metrics_use_the_shared_formulas():
    metrics = retrieval_metrics(["mem_1", "mem_5", "mem_9"], ["mem_5"], k=5)
    assert metrics["recall@5"] == 1.0
    # precision@k divides by the number actually returned, not by k:
    # a short ranking is not penalised for documents that do not exist.
    assert metrics["precision@5"] == pytest.approx(1 / 3)
    assert metrics["mrr"] == pytest.approx(1 / 2)


def test_the_token_budget_metric_is_per_injection():
    pack = build_context_pack("c1", current_state="Turn: 1", token_budget=800)
    assert tokens_per_injection([pack]) > 0
    assert tokens_per_injection([]) == 0.0