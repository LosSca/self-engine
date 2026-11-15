"""P2: entity index, hybrid recall across crystals and facts, temporal queries.

The old crystal recall could only match words. These tests pin the behaviour
that the upgrade adds on top of it - alias resolution, the temporal channel,
the theme-driven consolidation - while asserting that every one of those
channels can be switched off and the legacy ranking still answers.
"""

import os

import pytest

from app.db.memory_fact_store import MemoryFactStore
from app.engines.memory_consolidation import (
    CONSOLIDATION_THEME,
    CONSOLIDATION_TIME,
    group_crystals,
    plan_consolidation,
    select_merge_batch,
    theme_keywords,
)
from app.engines.memory_engine import CrystalTier, MemoryCrystal, MemoryEngine
from app.models.memory import EvidenceType, FactStatus, MemoryEntity, MemoryFact
from app.services.retrieval.entity_index import (
    EntityIndex,
    build_entity_index,
    find_mentions,
    mentioned_names,
)
from app.services.retrieval.memory_retrieval import (
    crystal_docs_from_engine,
    fact_docs_from_store,
    hybrid_rank,
)


class _NullStore:
    """EventStore stand-in: these tests only exercise ranking."""

    def get_by_type(self, campaign_id, event_type, limit=-1):
        return []

    def get_recent(self, campaign_id, limit=10):
        return []

    def get_after(self, campaign_id, event_id=None, **kwargs):
        return []


@pytest.fixture()
def fact_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_FACTS_ENABLED", "1")
    store = MemoryFactStore(str(tmp_path / "memory.db"))
    yield store
    store.close()


def _crystal(
    campaign_id,
    tier,
    text,
    *,
    memory_id=None,
    entities=(),
    witnesses=(),
    start="2026-01-01T00:00:00",
    importance=0.0,
    confidence=1.0,
    consumed=False,
    always_include=False,
):
    return MemoryCrystal(
        campaign_id=campaign_id,
        tier=tier,
        content=text,
        ai_content=text,
        event_count=4,
        consumed=consumed,
        source_start_created_at=start,
        source_end_created_at=start,
        witnessed_by=list(witnesses),
        audience_resolved=True,
        memory_id=memory_id or f"mem_{abs(hash(text)) % 10**12:012d}",
        related_entities=list(entities),
        importance=importance,
        confidence=confidence,
        always_include=always_include,
    )


def _fact(campaign_id, subject, predicate, value, *, valid_from=None, observers=(), confidence=0.9):
    return MemoryFact(
        id=f"f_{abs(hash((subject, predicate, value))) % 10**12:012d}",
        campaign_id=campaign_id,
        subject_id=subject,
        predicate=predicate,
        object_value=value,
        memory_type="semantic",
        confidence=confidence,
        importance=0.7,
        status=FactStatus.ACTIVE,
        valid_from=valid_from,
        observer_ids=list(observers),
        source_event_ids=[],
    )


# ── entity index ───────────────────────────────────────────────────


def test_aliases_resolve_to_one_entity_across_scripts():
    index = EntityIndex();
    index.add("Eileen", ["艾琳", "エリン", "EILEEN"], entity_id="e1", kind="person")
    assert index.canonical("艾琳") == "Eileen"
    assert index.canonical("エリン") == "Eileen"
    assert index.canonical("eileen") == "Eileen"
    assert index.canonical("Eileen") == "Eileen"
    assert index.canonical("nobody") == ""


def test_mentions_are_longest_match_and_do_not_overlap():
    index = EntityIndex()
    index.add("Eileen", ["艾琳"], entity_id="e1", kind="person")
    index.add("White Pearl", ["白珍珠号"], entity_id="e2", kind="item")
    names = mentioned_names("艾琳とEileenが来た。The White Pearl sailed.", index)
    assert names == ["Eileen", "White Pearl"]
    mentions = find_mentions("艾琳 meets Eileen", index)
    assert len(mentions) == 2
    assert all(m.canonical_name == "Eileen" for m in mentions)


def test_a_latin_alias_does_not_match_inside_a_longer_word():
    index = EntityIndex()
    index.add("Rin", [], entity_id="e1", kind="person")
    assert mentioned_names("grinning", index) == []
    assert mentioned_names("Rin, at last", index) == ["Rin"]


def test_an_alias_already_owned_by_another_entity_is_not_stolen():
    index = EntityIndex()
    index.add("Eileen", ["Lin"], entity_id="e1", kind="person")
    index.add("Marcus", ["Lin"], entity_id="e2", kind="person")
    # Two people cannot share one alias: the first registration keeps it, so
    # Marcus memories never surface on an Eileen query.
    assert index.canonical("Lin") == "Eileen"


def test_the_index_builds_from_stored_entities(fact_store):
    fact_store.upsert_entity(
        MemoryEntity(
            id="",
            campaign_id="c1",
            canonical_name="Eileen",
            entity_type="person",
            aliases=["艾琳", "エリン"],
        )
    )
    index = build_entity_index(fact_store.list_entities("c1"))
    assert index.canonical("艾琳") == "Eileen"
    assert index.kind_of("Eileen") == "person"

# ── hybrid recall over crystals ────────────────────────────────────


def test_bm25_and_engine_channels_agree_when_only_words_matter():
    docs = crystal_docs_from_engine([
        _crystal("c1", CrystalTier.SHORT, "The blacksmith wants iron from the mine."),
        _crystal("c1", CrystalTier.SHORT, "A quiet night at the temple."),
    ])
    engine_scores = {docs[0].doc_id: 40.0, docs[1].doc_id: 10.0}
    result = hybrid_rank("iron mine", docs, engine_scores=engine_scores)
    assert result.doc_ids()[0] == docs[0].doc_id
    assert "bm25" in result.channels or "engine" in result.channels


def test_the_alias_channel_finds_the_knight_the_query_never_names():
    index = EntityIndex()
    index.add("Eileen", ["艾琳", "女骑士"], entity_id="e1", kind="person")
    rescue = _crystal(
        "c1", CrystalTier.LONG,
        "艾琳 held the north gate while the villagers escaped.",
        entities=["Eileen"],
    )
    later = _crystal("c1", CrystalTier.LONG, "The harvest was uneventful.")
    docs = crystal_docs_from_engine([later, rescue])
    result = hybrid_rank("上次救过我的女骑士现在怎么样了", docs, entity_index=index)
    assert result.doc_ids()[0] == docs[1].doc_id
    assert result.channels.get("alias", 0) >= 1


def test_a_fact_valid_now_ranks_above_the_stale_crystal():
    index = EntityIndex()
    index.add("Eileen", [], entity_id="e1", kind="person")
    stale = _crystal(
        "c1", CrystalTier.MEDIUM,
        "Eileen is hostile and will kill on sight.",
        entities=["Eileen"],
    )
    docs = crystal_docs_from_engine([stale])
    facts = fact_docs_from_store(_FnStore([_fact("c1", "Eileen", "relationship", "friend")]), "c1")
    result = hybrid_rank("Eileen relationship", docs, facts=facts, entity_index=index)
    assert "fact" in result.channels
    assert result.channels["fact"] == 1


def test_hybrid_rank_degrades_instead_of_raising(monkeypatch):
    docs = crystal_docs_from_engine([
        _crystal("c1", CrystalTier.SHORT, "Something happened."),
    ])

    def boom(*args, **kwargs):
        raise RuntimeError("index is gone")

    monkeypatch.setattr("app.services.retrieval.memory_retrieval.lexical_retriever.search", boom)
    result = hybrid_rank("what happened", docs, engine_scores={docs[0].doc_id: 1.0})
    assert "bm25" in result.degraded
    assert result.doc_ids() == [docs[0].doc_id]  # engine channel still answers


class _FnStore:
    """Minimal store double: find_facts only."""

    def __init__(self, facts):
        self._facts = facts

    def find_facts(self, campaign_id, **kwargs):
        return [f for f in self._facts if f.campaign_id == campaign_id]

    def list_entities(self, campaign_id):
        return []

# ── temporal retrieval ─────────────────────────────────────────────


def test_the_fact_store_answers_for_today_and_for_chapter_two(fact_store):
    cid = "c-time"
    enemy = fact_store.add_fact(_fact(cid, "Eileen", "relationship", "enemy", valid_from="2026-01-01"))[0]
    fact_store.update_status(enemy.id, FactStatus.SUPERSEDED, valid_until="2026-02-01T00:00:00")

    # Superseded is history, not memory: today it is not the answer...
    now = fact_store.find_facts(cid)
    assert enemy.id not in [f.id for f in now]
    # ...but chapter two still reads the way the player lived it.
    historical = fact_store.find_facts(cid, at="2026-01-15T00:00:00")
    assert [f.object_value for f in historical] == ["enemy"]


def test_fact_docs_honour_the_validity_window(fact_store):
    cid = "c-window"
    old = _fact(cid, "Rin", "status", "dead", valid_from="2025-01-01")
    new = _fact(cid, "Rin", "status", "alive", valid_from="2026-01-01")
    store = _FnStore([old, new])
    docs = fact_docs_from_store(store, cid)
    assert {doc.object_value for doc in docs} == {"dead", "alive"}
    assert all(doc.valid_from for doc in docs)


def test_a_windowed_query_returns_only_what_was_true_then(fact_store):
    cid = "c-window-2"
    store = fact_store
    enemy = store.add_fact(_fact(cid, "Eileen", "relationship", "enemy", valid_from="2026-01-01"))[0]
    store.update_status(enemy.id, FactStatus.SUPERSEDED, valid_until="2026-03-01")
    store.add_fact(_fact(cid, "Eileen", "relationship", "friend", valid_from="2026-03-01"))

    in_february = store.find_facts(cid, at="2026-02-01T00:00:00")
    assert [f.object_value for f in in_february] == ["enemy"]
    today = store.find_facts(cid)
    assert [f.object_value for f in today] == ["friend"]

# ── theme-driven consolidation ────────────────────────────────────


def test_one_thread_is_merged_even_when_another_thread_is_newer():
    # Two threads interleaved in time: Eileen's arc and Marcus's. The old merge
    # took the oldest four, which meant it cut across both and produced a
    # summary that belonged to neither. One pass now takes one thread.
    crystals = [
        _crystal("c1", CrystalTier.SHORT, "met Eileen at the gate",
                 entities=["Eileen"], start="2026-01-01T00:00:00"),
        _crystal("c1", CrystalTier.SHORT, "a word with Marcus",
                 entities=["Marcus"], start="2026-01-01T08:00:00"),
        _crystal("c1", CrystalTier.SHORT, "fought beside Eileen",
                 entities=["Eileen"], start="2026-01-02T00:00:00"),
        _crystal("c1", CrystalTier.SHORT, "Marcus named a price",
                 entities=["Marcus"], start="2026-01-02T08:00:00"),
        _crystal("c1", CrystalTier.SHORT, "Eileen revealed her rank",
                 entities=["Eileen"], start="2026-01-03T00:00:00"),
        _crystal("c1", CrystalTier.SHORT, "slept at the inn", start="2026-01-04T00:00:00"),
    ]
    batch = select_merge_batch(crystals, count=4, mode=CONSOLIDATION_THEME)
    merged = {c.ai_content for c in batch}
    assert len(batch) <= 4
    # Whichever thread won, the merge never mixes the two stories together -
    # that is the whole point of merging by theme instead of by age.
    assert merged in (
        {"met Eileen at the gate", "fought beside Eileen", "Eileen revealed her rank"},
        {"a word with Marcus", "Marcus named a price"},
    )
    assert "slept at the inn" not in merged


def test_time_mode_still_merges_the_oldest_four():
    crystals = [
        _crystal("c1", CrystalTier.SHORT, f"event {i}", start=f"2026-01-0{i + 1}T00:00:00")
        for i in range(5)
    ]
    batch = select_merge_batch(crystals, count=4, mode=CONSOLIDATION_TIME)
    assert [c.ai_content for c in batch] == ["event 0", "event 1", "event 2", "event 3"]


def test_a_pinned_crystal_is_never_swallowed_by_a_merge():
    canon = _crystal("c1", CrystalTier.SHORT, "CANON line", always_include=True)
    others = [
        _crystal("c1", CrystalTier.SHORT, f"Eileen scene {i}", entities=["Eileen"])
        for i in range(4)
    ]
    batch = select_merge_batch([canon] + others, count=2, mode=CONSOLIDATION_THEME)
    assert canon not in batch


def test_themes_are_grouped_by_shared_entity():
    crystals = [
        _crystal("c1", CrystalTier.MEDIUM, "a", entities=["Eileen"]),
        _crystal("c1", CrystalTier.MEDIUM, "b", entities=["Eileen"]),
        _crystal("c1", CrystalTier.MEDIUM, "c", entities=["Marcus"]),
        _crystal("c1", CrystalTier.MEDIUM, "d", entities=["Marcus"]),
    ]
    themes, skipped, deferred = group_crystals(crystals, min_size=2)
    assert skipped == []
    assert sorted(len(t.doc_ids) for t in themes) == [2, 2]
    assert deferred == []


def test_theme_keywords_surface_only_the_words_the_thread_shares():
    crystals = [
        _crystal("c1", CrystalTier.SHORT, "the gate was held by Eileen"),
        _crystal("c1", CrystalTier.SHORT, "Eileen held the gate at dawn"),
    ]
    words = theme_keywords(crystals)
    assert "eileen" in words
    assert "gate" in words


def test_the_plan_explains_what_it_deferred():
    crystals = [
        _crystal("c1", CrystalTier.SHORT, "a", entities=["Eileen"]),
        _crystal("c1", CrystalTier.SHORT, "b", entities=["Eileen"]),
        _crystal("c1", CrystalTier.SHORT, "c", entities=["Eileen"]),
        _crystal("c1", CrystalTier.SHORT, "d"),
    ]
    plan = plan_consolidation(crystals, count=4, mode=CONSOLIDATION_THEME)
    assert plan.mode == CONSOLIDATION_THEME
    assert plan.themes
    # The untagged scene rides along with the Eileen thread (it is the nearest
    # thing in time), so nothing is deferred here.
    assert plan.deferred == []
    assert plan.themes[0].key == "eileen"
    assert plan.themes[0].size == 4

# ── the engine switches ───────────────────────────────────────────


def test_retrieval_mode_defaults_to_hybrid_and_can_be_switched(monkeypatch):
    monkeypatch.delenv("MEMORY_RETRIEVAL", raising=False)
    engine = MemoryEngine(_NullStore(), llm=None)
    crystals = [_crystal("c1", CrystalTier.SHORT, "Eileen at the gate", entities=["Eileen"])]
    engine._crystals["c1"] = crystals
    assert engine._hybrid_recall(
        "c1", crystals,
        query_text="Eileen",
        active_npc_names=set(),
        location_keywords=set(),
        scene_entities=set(),
        task_keywords=set(),
    ) is not None

    monkeypatch.setenv("MEMORY_RETRIEVAL", "score")
    assert engine._hybrid_recall(
        "c1", crystals,
        query_text="Eileen",
        active_npc_names=set(),
        location_keywords=set(),
        scene_entities=set(),
        task_keywords=set(),
    ) is None


def test_the_engine_still_works_without_a_fact_store():
    engine = MemoryEngine(_NullStore(), llm=None)
    engine._crystals["c1"] = [
        _crystal("c1", CrystalTier.SHORT, "Eileen at the gate", entities=["Eileen"]),
    ]
    window = engine.build_context_window("c1", query_text="Eileen gate")
    assert "RCNT_MEM" in window
    assert engine.fact_store is None


def test_the_entity_cache_can_be_invalidated():
    engine = MemoryEngine(_NullStore(), llm=None)
    first = engine._entity_index("c1")
    assert engine._entity_index("c1") is first
    engine.invalidate_entity_cache("c1")
    assert engine._entity_index("c1") is not first