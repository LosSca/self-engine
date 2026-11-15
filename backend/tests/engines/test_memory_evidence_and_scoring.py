"""Provenance-aware ranking and evidence typing (P0.2 - P0.4).

The old scorer added up keyword hits, an NPC bonus, a location bonus and
recency. Everything else a crystal knew about itself - how confident it was,
how important, who witnessed it - was stored and then ignored at ranking time,
so a pivotal one-off event could sink below three trivial memories that
happened to share a word with the query.

These tests pin the new behavior *and* the old one: MEMORY_SCORING=legacy must
still reproduce the previous order exactly, because the evaluation harness
compares the two on the same corpus.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.event_store import EventStore, EventType
from app.engines.memory_engine import (
    CrystalTier,
    MemoryCrystal,
    MemoryEngine,
    _evidence_type_for_sources,
)
from app.models.memory import EvidenceType


@pytest.fixture
def event_store(tmp_path):
    store = EventStore(str(tmp_path / "events.db"))
    yield store
    store.close()


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=json.dumps(
            {"ai_memory": "AI::compressed", "player_summary": "Summary."}
        )
    )
    return llm


@pytest.fixture
def engine(event_store, mock_llm):
    return MemoryEngine(event_store=event_store, llm=mock_llm)


def crystal(
    campaign_id,
    tier,
    ai_content,
    *,
    importance=0.0,
    confidence=1.0,
    related_entities=None,
    witnessed_by=None,
    evidence_type="",
    always_include=False,
):
    return MemoryCrystal(
        campaign_id=campaign_id,
        tier=tier,
        content="s",
        ai_content=ai_content,
        event_count=1,
        importance=importance,
        confidence=confidence,
        related_entities=list(related_entities or []),
        witnessed_by=list(witnessed_by or []),
        audience_resolved=True,
        evidence_type=evidence_type,
        always_include=always_include,
    )


# ── ranking: provenance signals ──────────────────────────────────────


def test_importance_beats_a_single_keyword_hit(engine, monkeypatch):
    """A pivotal event must outrank a trivial one that shares one word."""
    cid = "c-importance"
    trivial = crystal(cid, CrystalTier.SHORT, "Player cooked fish at the harbor.")
    pivotal = crystal(
        cid, CrystalTier.SHORT, "The queen died at dawn.", importance=0.95
    )
    engine._crystals[cid] = [trivial, pivotal]

    # tier_size=1 pins the recency term to the same value for both, so the
    # comparison is about provenance and nothing else.
    keywords = {"cooked"}
    assert engine._score_crystal(pivotal, 0, 1, keywords, set(), set()) > engine._score_crystal(
        trivial, 0, 1, keywords, set(), set()
    )

    # The legacy formula, on the same crystals, ranks the keyword hit first.
    monkeypatch.setenv("MEMORY_SCORING", "legacy")
    legacy_trivial = engine._score_crystal(trivial, 0, 1, keywords, set(), set())
    legacy_pivotal = engine._score_crystal(pivotal, 0, 1, keywords, set(), set())
    assert legacy_trivial > legacy_pivotal


def test_confidence_breaks_a_tie_between_identical_contents(engine, monkeypatch):
    cid = "c-confidence"
    certain = crystal(cid, CrystalTier.MEDIUM, "Rin guards the north gate.", confidence=1.0)
    guess = crystal(cid, CrystalTier.MEDIUM, "Rin guards the north gate.", confidence=0.2)
    engine._crystals[cid] = [certain, guess]

    query = {"rin", "gate"}
    hybrid_certain = engine._score_crystal(certain, 0, 1, query, set(), set())
    hybrid_guess = engine._score_crystal(guess, 0, 1, query, set(), set())
    assert hybrid_certain - hybrid_guess == pytest.approx(
        engine._CONFIDENCE_WEIGHT * (1.0 - 0.2)
    )

    # Legacy cannot tell them apart at all: confidence was never read.
    monkeypatch.setenv("MEMORY_SCORING", "legacy")
    legacy_certain = engine._score_crystal(certain, 0, 1, query, set(), set())
    legacy_guess = engine._score_crystal(guess, 0, 1, query, set(), set())
    assert legacy_certain == pytest.approx(legacy_guess)


def test_related_entities_matched_against_the_scene(engine):
    cid = "c-entities"
    unrelated = crystal(cid, CrystalTier.SHORT, "A quiet morning in the market.")
    related = crystal(
        cid, CrystalTier.SHORT, "A quiet morning in the market.",
        related_entities=["Eileen", "Yumi"],
    )
    plain = engine._score_crystal(related, 0, 1, set(), set(), set())
    boosted = engine._score_crystal(
        related, 0, 1, set(), set(), set(), scene_entities={"eileen"}
    )
    assert boosted - plain == pytest.approx(engine._ENTITY_MATCH_SCORE)
    assert engine._score_crystal(
        unrelated, 0, 1, set(), set(), set(), scene_entities={"eileen"}
    ) == pytest.approx(
        engine._score_crystal(unrelated, 0, 1, set(), set(), set())
    )


def test_a_witnessed_memory_gets_the_observer_bonus(engine):
    cid = "c-observer"
    seen = crystal(cid, CrystalTier.SHORT, "Someone argued in the square.", witnessed_by=["Rin"])
    told = crystal(cid, CrystalTier.SHORT, "Someone argued in the square.")
    # "Someone argued in the square." does not contain the name, so the only
    # difference is the witness list.
    base_seen = engine._score_crystal(seen, 0, 1, set(), set(), set())
    assert engine._score_crystal(seen, 0, 1, set(), {"rin"}, set()) - base_seen == pytest.approx(
        engine._OBSERVER_BONUS
    )
    # Being told about it is not the same as having been there.
    base_told = engine._score_crystal(told, 0, 1, set(), set(), set())
    assert engine._score_crystal(told, 0, 1, set(), {"rin"}, set()) == pytest.approx(base_told)


def test_task_keywords_add_a_bounded_match(engine):
    cid = "c-task"
    target = crystal(cid, CrystalTier.MEDIUM, "The blacksmith wants iron from the mine.")
    base = engine._score_crystal(target, 0, 1, set(), set(), set())
    with_task = engine._score_crystal(
        target, 0, 1, set(), set(), set(), task_keywords={"iron", "mine", "forge", "anvil"}
    )
    assert with_task - base == pytest.approx(2 * engine._TASK_MATCH_SCORE)


# ── permanent tier: relevance instead of "everything, every turn" ───


def test_permanent_tier_is_no_longer_injected_whole(engine):
    cid = "c-permanent"
    # The Eileen line carries importance, because a bare keyword hit ties with
    # the recency bonus of the newest crystal and the tie would be noise.
    engine._crystals[cid] = [
        crystal(
            cid, CrystalTier.MEMORY,
            "PERMANENT one about Eileen the knight.", importance=0.8,
        ),
        crystal(cid, CrystalTier.MEMORY, "PERMANENT two about the northern swamp."),
        crystal(cid, CrystalTier.MEMORY, "PERMANENT three about a merchant guild."),
    ]
    # The query text is what the hybrid channels (bm25 + alias) rank against;
    # query_keywords alone still drives the legacy score.
    selected = engine._select_permanent_crystals(
        cid,
        query_text="what do I know about Eileen the knight",
        query_keywords={"eileen", "knight"},
        active_npc_names=set(),
        location_keywords=set(),
        scene_entities=set(),
        task_keywords=set(),
        token_budget=12,
        rag_on=True,
    )
    assert [c.ai_content for c in selected] == ["PERMANENT one about Eileen the knight."]
    assert len(selected) == 1  # the other two are not "permanent, therefore always"


def test_pinned_permanent_memory_survives_any_budget(engine):
    cid = "c-pinned"
    canon = crystal(cid, CrystalTier.MEMORY, "CANON: the world has two moons.", always_include=True)
    other = crystal(cid, CrystalTier.MEMORY, "Something about a swamp.")
    engine._crystals[cid] = [canon, other]

    selected = engine._select_permanent_crystals(
        cid,
        query_keywords=set(),
        active_npc_names=set(),
        location_keywords=set(),
        scene_entities=set(),
        task_keywords=set(),
        token_budget=0,
        rag_on=True,
    )
    assert canon in selected


def test_without_a_query_the_whole_tier_is_kept(engine):
    cid = "c-noquery"
    engine._crystals[cid] = [
        crystal(cid, CrystalTier.MEMORY, f"PERMANENT-{i}") for i in range(5)
    ]
    context = engine.build_context_window(cid)
    for i in range(5):
        assert f"PERMANENT-{i}" in context


# ── evidence typing ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_narration_only_crystal_carries_narrative_evidence(engine, event_store):
    event_store.append("c1", EventType.NARRATOR_RESPONSE, {"text": "Rain fell all night."}, 60, "loc", [])
    created = await engine.crystallize_short("c1")
    assert created.evidence_type == EvidenceType.LLM_NARRATIVE.value


@pytest.mark.asyncio
async def test_engine_settled_crystal_carries_action_evidence(engine, event_store):
    event_store.append("c1", EventType.PLAYER_ACTION, {"text": "I strike the wolf."}, 60, "loc", [])
    event_store.append("c1", EventType.COMBAT_RESULT, {"text": "The wolf dies."}, 60, "loc", [])
    created = await engine.crystallize_short("c1")
    assert created.evidence_type == EvidenceType.COMMITTED_ACTION.value


@pytest.mark.asyncio
async def test_mixed_sources_take_the_weakest_evidence(engine, event_store):
    """One prose source is enough to demote the whole memory to prose."""
    event_store.append("c1", EventType.PLAYER_ACTION, {"text": "I open the door."}, 60, "loc", [])
    event_store.append("c1", EventType.NARRATOR_RESPONSE, {"text": "The hall was cold."}, 60, "loc", [])
    created = await engine.crystallize_short("c1")
    assert created.evidence_type == EvidenceType.LLM_NARRATIVE.value


def test_evidence_type_of_a_legacy_crystal_is_narrative():
    legacy = SimpleNamespace(tier=CrystalTier.SHORT)
    assert _evidence_type_for_sources([legacy]) == EvidenceType.LLM_NARRATIVE.value


def test_evidence_type_of_a_consolidated_crystal_comes_from_its_parents():
    parents = [
        SimpleNamespace(evidence_type=EvidenceType.COMMITTED_ACTION.value),
        SimpleNamespace(evidence_type=EvidenceType.COMMITTED_ACTION.value),
    ]
    assert _evidence_type_for_sources(parents) == EvidenceType.COMMITTED_ACTION.value


def test_evidence_type_of_no_sources_is_the_safe_assumption():
    assert _evidence_type_for_sources([]) == EvidenceType.LLM_NARRATIVE.value


def test_memory_type_maps_the_crystal_ladder():
    cid = "c-types"
    assert crystal(cid, CrystalTier.SHORT, "x").memory_type == "episodic"
    assert crystal(cid, CrystalTier.LONG, "x").memory_type == "episodic"
    assert crystal(cid, CrystalTier.MEMORY, "x").memory_type == "semantic"
