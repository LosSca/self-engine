"""The memory ingestion pipeline and the fact store (P1).

The point of these tests is not coverage in the abstract. Each one pins a way
the old system could be poisoned or could forget:

* prose wrote itself into the world's truth;
* a player's guess became a fact;
* a rumour counted as canon;
* an engine settlement was lost because a summary had been written over it;
* re-running extraction duplicated facts until importance was meaningless;
* a superseded relationship still answered "what is true now".
"""
import json
from unittest.mock import AsyncMock

import pytest

from app.db.event_store import Event, EventStore, EventType
from app.db.memory_fact_store import MemoryFactStore, fact_fingerprint
from app.models.memory import (
    EvidenceType,
    FactStatus,
    MemoryEntity,
    MemoryFact,
    MemoryRelation,
    can_transition,
    transition_path,
)
from app.services.memory_ingestion import (
    DeterministicExtractor,
    FactProposal,
    IngestReport,
    LLMFactExtractor,
    MemoryIngestionPipeline,
)


@pytest.fixture
def event_store(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    yield store
    store.close()


@pytest.fixture
def fact_store(tmp_path):
    store = MemoryFactStore(str(tmp_path / "memory.db"))
    yield store
    store.close()


@pytest.fixture
def pipeline(event_store, fact_store):
    return MemoryIngestionPipeline(event_store, fact_store)


def _event(event_store, campaign, kind, payload, location="square", witnessed=None):
    return event_store.append(
        campaign_id=campaign,
        event_type=kind,
        payload=payload,
        narrative_time_delta=0,
        location=location,
        entities=[],
        witnessed_by=witnessed or [],
    )


def _event_obj(kind=EventType.NARRATOR_RESPONSE, payload=None, witnessed=None):
    return Event(
        id="e1",
        campaign_id="c1",
        event_type=kind,
        payload=payload or {"text": "x"},
        narrative_time_delta=0,
        location="",
        entities=[],
        created_at="2026-01-01T00:00:00",
        witnessed_by=witnessed or [],
    )


def _draft(fact_store, **kwargs):
    """A fact parked at CANDIDATE, which is where the pipeline starts them."""
    allowed = {"subject_id", "predicate", "object_value", "evidence_type", "valid_from"}
    field_names = {"subject_id", "predicate", "object_value", "evidence_type", "valid_from", "valid_until"}
    dropped = {k: v for k, v in kwargs.items() if k not in field_names}
    kept = {k: v for k, v in kwargs.items() if k in field_names}
    fact = MemoryFact(campaign_id="c1", **kept)
    stored, _ = fact_store.add_fact(fact)
    if stored.status == FactStatus.EXTRACTED.value:
        stored = fact_store.update_status(stored.id, FactStatus.CANDIDATE.value)
    return stored, dropped


# -- fact accuracy: what may become world truth ----------------------


@pytest.mark.asyncio
async def test_engine_settlement_writes_an_active_fact(pipeline, event_store, fact_store):
    _event(event_store, "c1", EventType.INVENTORY, {"action": "add", "name": "Iron Sword"})
    report = await pipeline.ingest("c1")

    facts = fact_store.find_facts("c1", predicate="carrying")
    assert len(facts) == 1
    assert facts[0].object_value == "Iron Sword"
    assert facts[0].status == FactStatus.ACTIVE.value
    assert facts[0].evidence_type == EvidenceType.ENGINE_STATE.value
    assert report.activated == 1


@pytest.mark.asyncio
async def test_narrator_prose_cannot_become_world_truth(fact_store, event_store):
    """A confident model claim about prose still stops at CANDIDATE."""
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=json.dumps(
            [{"subject": "Eileen", "predicate": "secretly_owns", "object": "the northern fleet",
              "confidence": 1.0, "importance": 1.0}]
        )
    )
    pipeline = MemoryIngestionPipeline(event_store, fact_store, llm=llm)
    _event(event_store, "c1", EventType.NARRATOR_RESPONSE, {"text": "Eileen smiled at a ship."})
    await pipeline.ingest("c1")

    all_versions = fact_store.find_facts("c1", predicate="secretly_owns", statuses=None)
    assert len(all_versions) == 1
    assert all_versions[0].status == FactStatus.CANDIDATE.value
    assert all_versions[0].evidence_type == EvidenceType.LLM_NARRATIVE.value
    # And it is not readable as current truth.
    assert fact_store.find_facts("c1", predicate="secretly_owns") == []


@pytest.mark.asyncio
async def test_player_statement_is_not_world_knowledge(pipeline, fact_store):
    """A player's claim about their background is a statement, not a fact."""
    proposal = FactProposal(
        subject_id="player",
        predicate="noble_birth",
        object_value="the third prince",
        evidence_type=EvidenceType.PLAYER_STATEMENT.value,
        confidence=0.9,
    )
    report = IngestReport(campaign_id="c1")
    fact = pipeline.commit("c1", proposal, report)
    assert fact is not None
    assert fact.status == FactStatus.CANDIDATE.value
    assert fact_store.find_facts("c1", predicate="noble_birth") == []


@pytest.mark.asyncio
async def test_npc_rumour_is_not_canon(pipeline, fact_store):
    proposal = FactProposal(
        subject_id="Duke",
        predicate="relationship",
        object_value="traitor",
        evidence_type=EvidenceType.NPC_DIALOGUE.value,
        observer_ids=["Rin"],
        confidence=0.6,
    )
    pipeline.commit("c1", proposal, IngestReport(campaign_id="c1"))
    fact = fact_store.find_facts("c1", predicate="relationship", statuses=None)[0]
    assert fact.status == FactStatus.CANDIDATE.value
    assert fact.evidence_type == EvidenceType.NPC_DIALOGUE.value


@pytest.mark.asyncio
async def test_an_observation_without_an_observer_is_never_authoritative(pipeline, fact_store):
    proposal = FactProposal(
        subject_id="Eileen",
        predicate="mood",
        object_value="furious",
        evidence_type=EvidenceType.NPC_OBSERVATION.value,
        confidence=1.0,
    )
    pipeline.commit("c1", proposal, IngestReport(campaign_id="c1"))
    fact = fact_store.find_facts("c1", predicate="mood", statuses=None)[0]
    assert fact.status == FactStatus.CANDIDATE.value


@pytest.mark.asyncio
async def test_two_independent_observations_verify_a_claim(pipeline, fact_store):
    for observer, event_id in (("Rin", "e1"), ("Yumi", "e2")):
        pipeline.commit(
            "c1",
            FactProposal(
                subject_id="Eileen",
                predicate="mood",
                object_value="calm",
                evidence_type=EvidenceType.NPC_OBSERVATION.value,
                observer_ids=[observer],
                confidence=0.9,
                source_event_id=event_id,
            ),
            IngestReport(campaign_id="c1"),
        )
    fact = fact_store.find_facts("c1", predicate="mood", statuses=None)[0]
    assert fact.status == FactStatus.VERIFIED.value


# -- state machine ---------------------------------------------------


def test_illegal_transitions_are_refused(fact_store):
    fact, _ = fact_store.add_fact(
        MemoryFact(campaign_id="c1", subject_id="A", predicate="p", object_value="v")
    )
    # extracted -> active may not be taken in one hop.
    with pytest.raises(ValueError):
        fact_store.update_status(fact.id, FactStatus.ACTIVE.value)


def test_rejected_facts_stay_rejected(fact_store):
    fact, _ = fact_store.add_fact(
        MemoryFact(campaign_id="c1", subject_id="A", predicate="p", object_value="v")
    )
    fact_store.update_status(fact.id, FactStatus.REJECTED.value)
    with pytest.raises(ValueError):
        fact_store.update_status(fact.id, FactStatus.CANDIDATE.value)


def test_the_lifecycle_has_one_meaning():
    assert transition_path(FactStatus.EXTRACTED.value, FactStatus.ACTIVE.value) == [
        "extracted",
        "candidate",
        "verified",
        "active",
    ]
    # An active fact can be superseded, disputed or retracted - but a claim is
    # never thrown away later than that: rejection is for claims that never
    # reached the world, so it is only reachable before ACTIVE.
    assert transition_path("active", "rejected") == ["active", "disputed", "rejected"]
    # Rejection is never taken in one hop from ACTIVE, so a claim that reached
    # the world has to be disputed before it can be thrown out.
    assert not can_transition("active", "rejected")
    assert "superseded" in transition_path("active", "superseded")
    assert can_transition("candidate", "verified")


def test_a_superseded_fact_is_closed_not_deleted(fact_store):
    old, _ = fact_store.add_fact(
        MemoryFact(campaign_id="c1", subject_id="Eileen", predicate="relationship",
                   object_value="enemy", evidence_type=EvidenceType.ENGINE_STATE.value,
                   valid_from="2026-01-01T00:00:00")
    )
    fact_store.advance_status(old.id, FactStatus.ACTIVE.value)
    new, _ = fact_store.add_fact(
        MemoryFact(campaign_id="c1", subject_id="Eileen", predicate="relationship",
                   object_value="friend", evidence_type=EvidenceType.ENGINE_STATE.value,
                   valid_from="2026-01-03T00:00:00")
    )
    fact_store.advance_status(new.id, FactStatus.ACTIVE.value)
    fact_store.supersede(old.id, new.id)

    stored_old = fact_store.get_fact(old.id)
    assert stored_old is not None
    assert stored_old.status == FactStatus.SUPERSEDED.value
    assert stored_old.valid_until == "2026-01-03T00:00:00"


# -- temporal reasoning ----------------------------------------------


def _relationship_story(fact_store):
    old, _ = fact_store.add_fact(
        MemoryFact(campaign_id="c1", subject_id="Eileen", predicate="relationship",
                   object_value="enemy", evidence_type=EvidenceType.ENGINE_STATE.value,
                   valid_from="2026-01-01T00:00:00")
    )
    fact_store.advance_status(old.id, FactStatus.ACTIVE.value)
    new, _ = fact_store.add_fact(
        MemoryFact(campaign_id="c1", subject_id="Eileen", predicate="relationship",
                   object_value="friend", evidence_type=EvidenceType.ENGINE_STATE.value,
                   valid_from="2026-01-03T00:00:00")
    )
    fact_store.advance_status(new.id, FactStatus.ACTIVE.value)
    fact_store.supersede(old.id, new.id)


def test_current_query_returns_only_the_live_relationship(fact_store):
    _relationship_story(fact_store)
    current = [f.object_value for f in fact_store.find_facts("c1", predicate="relationship")]
    assert current == ["friend"]


def test_historical_query_returns_what_was_true_then(fact_store):
    _relationship_story(fact_store)
    early = [f.object_value for f in fact_store.find_facts(
        "c1", predicate="relationship", at="2026-01-02T00:00:00")]
    late = [f.object_value for f in fact_store.find_facts(
        "c1", predicate="relationship", at="2026-01-04T00:00:00")]
    assert early == ["enemy"]
    assert late == ["friend"]


def test_a_retracted_fact_disappears_from_history_too(fact_store):
    fact, _ = fact_store.add_fact(
        MemoryFact(campaign_id="c1", subject_id="A", predicate="location",
                   object_value="harbor", evidence_type=EvidenceType.ENGINE_STATE.value,
                   valid_from="2026-01-01T00:00:00")
    )
    fact_store.advance_status(fact.id, FactStatus.ACTIVE.value)
    fact_store.update_status(fact.id, FactStatus.RETRACTED.value)
    assert fact_store.find_facts("c1", at="2026-01-01T00:00:00") == []


# -- idempotence and repeat consumption ------------------------------


@pytest.mark.asyncio
async def test_the_same_event_twice_does_not_duplicate_or_inflate(pipeline, event_store, fact_store):
    _event(event_store, "c1", EventType.INVENTORY, {"action": "add", "name": "Rope"})
    await pipeline.ingest("c1")
    await pipeline.ingest("c1", after="")  # replay the very same events

    facts = fact_store.find_facts("c1", predicate="carrying")
    assert len(facts) == 1
    assert fact_store.count_evidence(facts[0].id) == 1
    assert facts[0].importance <= 0.6, "repeat consumption must not inflate it"


@pytest.mark.asyncio
async def test_the_cursor_advances_so_a_second_run_finds_nothing(pipeline, event_store):
    _event(event_store, "c1", EventType.INVENTORY, {"action": "add", "name": "Rope"})
    first = await pipeline.ingest("c1")
    second = await pipeline.ingest("c1")
    assert first.consumed_events == 1
    assert second.consumed_events == 0


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(pipeline, event_store, fact_store):
    _event(event_store, "c1", EventType.INVENTORY, {"action": "add", "name": "Rope"})
    report = await pipeline.ingest("c1", dry_run=True)
    assert report.activated == 1
    assert fact_store.overview("c1")["facts"] == 0
    assert fact_store.get_cursor("c1")["last_event_created_at"] == ""


@pytest.mark.asyncio
async def test_a_failing_extractor_degrades_instead_of_aborting(fact_store, event_store):
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=RuntimeError("model down"))
    pipeline = MemoryIngestionPipeline(event_store, fact_store, llm=llm)
    _event(event_store, "c1", EventType.INVENTORY, {"action": "add", "name": "Rope"})
    _event(event_store, "c1", EventType.NARRATOR_RESPONSE, {"text": "Rain."})

    report = await pipeline.ingest("c1")
    assert report.extractor_errors, "the failure must be reported"
    # The deterministic fact still landed: no model needed for it.
    assert [f.object_value for f in fact_store.find_facts("c1", predicate="carrying")] == ["Rope"]


@pytest.mark.asyncio
async def test_original_events_are_never_modified(pipeline, event_store):
    _event(event_store, "c1", EventType.INVENTORY, {"action": "add", "name": "Rope"})
    before = event_store.get_recent("c1", limit=10)
    await pipeline.ingest("c1")
    after = event_store.get_recent("c1", limit=10)
    assert [(e.id, e.payload, e.created_at) for e in before] == [
        (e.id, e.payload, e.created_at) for e in after
    ]


# -- conflict detection ----------------------------------------------


@pytest.mark.asyncio
async def test_a_new_location_supersedes_the_old_one(pipeline, event_store, fact_store):
    _event(event_store, "c1", EventType.PLAYER_ACTION, {"text": "I walk north."}, location="north gate")
    _event(event_store, "c1", EventType.PLAYER_ACTION, {"text": "I walk south."}, location="harbor")
    await pipeline.ingest("c1")

    current = [f.object_value for f in fact_store.find_facts("c1", predicate="current_location")]
    assert current == ["harbor"]
    history = fact_store.find_facts("c1", predicate="current_location", statuses=None)
    # The old location is closed, not erased: it stays as history.
    assert any(f.status == FactStatus.SUPERSEDED.value for f in history)


@pytest.mark.asyncio
async def test_a_second_combat_does_not_erase_the_first(pipeline, event_store, fact_store):
    """Events accumulate; only state-like predicates are replaced."""
    _event(event_store, "c1", EventType.COMBAT_RESULT,
           {"opponent_name": "Wolf", "outcome": "win", "quality": 9, "opponent_power": 2},
           location="forest")
    _event(event_store, "c1", EventType.COMBAT_RESULT,
           {"opponent_name": "Bear", "outcome": "flee", "quality": 3, "opponent_power": 7},
           location="forest")
    await pipeline.ingest("c1")

    outcomes = {f.object_value for f in fact_store.find_facts("c1", predicate="combat_outcome")}
    assert outcomes == {"win vs Wolf", "flee vs Bear"}, outcomes


@pytest.mark.asyncio
async def test_power_level_updates_replace_each_other(pipeline, event_store, fact_store):
    _event(event_store, "c1", EventType.POWER_LEVEL_UPDATE, {"target": "player", "new_power": 4})
    _event(event_store, "c1", EventType.POWER_LEVEL_UPDATE, {"target": "player", "new_power": 5})
    await pipeline.ingest("c1")
    current = [f.object_value for f in fact_store.find_facts("c1", predicate="power_level")]
    assert current == ["5"]


# -- evidence and provenance -----------------------------------------


@pytest.mark.asyncio
async def test_every_fact_points_back_at_its_event(pipeline, event_store, fact_store):
    event = _event(event_store, "c1", EventType.INVENTORY, {"action": "add", "name": "Rope"})
    await pipeline.ingest("c1")
    fact = fact_store.find_facts("c1", predicate="carrying")[0]
    assert event.id in fact.source_event_ids
    evidence = fact_store.get_evidence(fact.id)
    assert [item.source_event_id for item in evidence] == [event.id]
    assert evidence[0].evidence_content


@pytest.mark.asyncio
async def test_observers_are_recorded_on_the_fact(fact_store, event_store):
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=json.dumps(
            [{"subject": "Eileen", "predicate": "saw_ritual", "object": "yes",
              "confidence": 0.8, "importance": 0.6, "observer": "Rin"}]
        )
    )
    pipeline = MemoryIngestionPipeline(event_store, fact_store, llm=llm)
    _event(event_store, "c1", EventType.NARRATOR_RESPONSE, {"text": "Rin watched."}, witnessed=["Rin"])
    await pipeline.ingest("c1")

    facts = fact_store.find_facts("c1", statuses=None)
    assert facts, "the proposal should have been stored as a candidate"
    assert facts[0].observer_ids == ["Rin"]


# -- entities --------------------------------------------------------


def test_entity_aliases_resolve_to_one_entity(fact_store):
    fact_store.upsert_entity(
        MemoryEntity(campaign_id="c1", canonical_name="Eileen", entity_type="person",
                     aliases=["艾琳", "エリン"])
    )
    for alias in ("艾琳", "エリン", "EILEEN", " eileen "):
        resolved = fact_store.resolve_entity("c1", alias)
        assert resolved is not None and resolved.canonical_name == "Eileen", alias


def test_upserting_an_entity_merges_aliases(fact_store):
    fact_store.upsert_entity(MemoryEntity(campaign_id="c1", canonical_name="Eileen", aliases=["艾琳"]))
    merged = fact_store.upsert_entity(
        MemoryEntity(campaign_id="c1", canonical_name="Eileen", aliases=["Erin"]))
    assert set(merged.aliases) == {"艾琳", "Erin"}


def test_relations_are_scoped_to_their_window(fact_store):
    fact_store.add_relation(
        MemoryRelation(campaign_id="c1", subject_id="a", predicate="sworn_to", object_id="b",
                       valid_from="2026-01-01T00:00:00", valid_until="2026-01-05T00:00:00")
    )
    assert len(fact_store.get_relations("c1", at="2026-01-02T00:00:00")) == 1
    assert fact_store.get_relations("c1", at="2026-01-06T00:00:00") == []


# -- extractor parsing -----------------------------------------------


def test_the_llm_parser_rejects_half_formed_triples():
    extractor = LLMFactExtractor(AsyncMock())
    proposals = extractor.parse(
        json.dumps([{"subject": "", "predicate": "p", "object": "v"},
                    {"subject": "A", "predicate": "", "object": "v"},
                    {"subject": "A", "predicate": "p", "object": "v"}]),
        _event_obj(),
    )
    assert len(proposals) == 1
    assert proposals[0].subject_id == "A"


def test_the_llm_parser_survives_prose_around_the_json():
    extractor = LLMFactExtractor(AsyncMock())
    raw = 'Here you go:\n[{"subject": "A", "predicate": "is", "object": "b"}]\nDone.'
    assert len(extractor.parse(raw, _event_obj())) == 1


def test_garbage_output_yields_no_proposals():
    extractor = LLMFactExtractor(AsyncMock())
    assert extractor.parse("I cannot help with that.", _event_obj()) == []
    assert extractor.parse(None, _event_obj()) == []


def test_deterministic_extraction_ignores_malformed_payloads():
    extractor = DeterministicExtractor()
    event = _event_obj(EventType.COMBAT_RESULT, {"opponent_name": "Wolf"})
    assert extractor.extract(event) == []


# -- fingerprint identity --------------------------------------------


def test_fingerprint_includes_the_validity_window():
    base = fact_fingerprint("c1", "Eileen", "relationship", "enemy")
    other_window = fact_fingerprint("c1", "Eileen", "relationship", "enemy", "ch3")
    assert base != other_window
    # Same content, different spelling => same fact.
    assert fact_fingerprint("c1", "eileen ", "Relationship", "ENEMY") == base
