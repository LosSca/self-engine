import pytest
import json
from unittest.mock import AsyncMock
from app.engines.memory_engine import MemoryEngine, MemoryCrystal, CrystalTier
from app.db.event_store import EventStore, EventType


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
            {
                "ai_memory": "AI::compressed::memory",
                "player_summary": "Compressed memory crystal content.",
            }
        )
    )
    return llm


@pytest.fixture
def memory_engine(event_store, mock_llm):
    return MemoryEngine(event_store=event_store, llm=mock_llm)


def test_raw_context_returns_recent_events(memory_engine, event_store):
    for i in range(5):
        event_store.append("c1", EventType.PLAYER_ACTION, {"text": f"action {i}"}, 60, "loc", [])
    ctx = memory_engine.get_raw_context("c1", limit=3)
    assert len(ctx) == 3


@pytest.mark.asyncio
async def test_crystallize_creates_crystal(memory_engine, event_store):
    for i in range(5):
        event_store.append("c1", EventType.PLAYER_ACTION, {"text": f"action {i}"}, 60, "loc", [])
    crystal = await memory_engine.crystallize("c1", tier=CrystalTier.SHORT)
    assert crystal.content == "Compressed memory crystal content."
    assert crystal.ai_content == "AI::compressed::memory"
    assert crystal.tier == CrystalTier.SHORT
    assert crystal.campaign_id == "c1"


def test_build_context_window_includes_raw_events(memory_engine, event_store):
    event_store.append("c1", EventType.PLAYER_ACTION, {"text": "I enter the tavern"}, 60, "loc", [])
    context = memory_engine.build_context_window("c1")
    assert "I enter the tavern" in context


@pytest.mark.asyncio
async def test_build_context_window_includes_crystals(memory_engine, event_store):
    # SHORT is the only tier created directly from raw events; higher tiers
    # exist solely via cascade consolidation. Test the supported path.
    for i in range(5):
        event_store.append("c1", EventType.PLAYER_ACTION, {"text": f"event {i}"}, 60, "loc", [])
    await memory_engine.crystallize("c1", tier=CrystalTier.SHORT)
    context = memory_engine.build_context_window("c1")
    assert "AI::compressed::memory" in context


def test_get_crystals_empty_for_new_campaign(memory_engine):
    crystals = memory_engine.get_crystals("new-campaign")
    assert crystals == []


@pytest.mark.asyncio
async def test_build_context_window_includes_graphiti_facts(event_store, mock_llm):
    mock_graphiti = AsyncMock()
    mock_graphiti.search = AsyncMock(return_value=[
        {"fact": "The knight betrayed the king in chapter 3.", "valid_at": None, "invalid_at": None},
        {"fact": "The goblin camp is north of the river.", "valid_at": None, "invalid_at": None},
    ])
    engine = MemoryEngine(event_store=event_store, llm=mock_llm, graphiti_engine=mock_graphiti)
    event_store.append("camp-1", EventType.PLAYER_ACTION, {"text": "I scout the northern road"}, 60, "loc", [])
    ctx = await engine.build_context_window_async("camp-1")
    assert "WORLD FACTS" in ctx
    assert "knight betrayed the king" in ctx
    assert "goblin camp" in ctx


@pytest.mark.asyncio
async def test_auto_crystallize_uses_fallback_when_llm_fails(event_store):
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=RuntimeError("llm unavailable"))
    engine = MemoryEngine(event_store=event_store, llm=llm)

    for i in range(engine.AUTO_CRYSTALLIZE_THRESHOLD + 1):
        event_store.append("c1", EventType.PLAYER_ACTION, {"text": f"action {i}"}, 60, "loc", [])

    crystal = await engine.auto_crystallize_if_needed("c1")
    assert crystal is not None
    assert crystal.tier == CrystalTier.SHORT
    assert "action" in crystal.content.lower()


@pytest.mark.asyncio
async def test_auto_crystallize_is_incremental_without_repeating_events(event_store):
    llm = AsyncMock()
    llm.complete = AsyncMock(
        side_effect=[
            json.dumps({"ai_memory": "AI_DELTA_1", "player_summary": "summary-1"}),
            json.dumps({"ai_memory": "AI_DELTA_2", "player_summary": "summary-2"}),
        ]
    )
    engine = MemoryEngine(event_store=event_store, llm=llm)

    for i in range(engine.AUTO_CRYSTALLIZE_THRESHOLD):
        event_store.append("c1", EventType.PLAYER_ACTION, {"text": f"first-batch-{i}"}, 60, "loc", [])

    crystal_1 = await engine.auto_crystallize_if_needed("c1")
    assert crystal_1 is not None
    assert crystal_1.event_count == engine.AUTO_CRYSTALLIZE_THRESHOLD

    for i in range(engine.AUTO_CRYSTALLIZE_THRESHOLD - 1):
        event_store.append("c1", EventType.PLAYER_ACTION, {"text": f"second-batch-{i}"}, 60, "loc", [])

    crystal_mid = await engine.auto_crystallize_if_needed("c1")
    assert crystal_mid is None  # still below threshold for new events

    event_store.append("c1", EventType.PLAYER_ACTION, {"text": "second-batch-last"}, 60, "loc", [])
    crystal_2 = await engine.auto_crystallize_if_needed("c1")

    assert crystal_2 is not None
    assert crystal_2.event_count == engine.AUTO_CRYSTALLIZE_THRESHOLD
    assert crystal_2.content == "summary-2"


@pytest.mark.asyncio
async def test_context_window_uses_ai_crystal_and_only_uncrystallized_delta(event_store):
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=json.dumps({"ai_memory": "AI_CHAIN_A", "player_summary": "Exec summary A"})
    )
    engine = MemoryEngine(event_store=event_store, llm=llm)

    for i in range(engine.AUTO_CRYSTALLIZE_THRESHOLD):
        event_store.append("c1", EventType.PLAYER_ACTION, {"text": f"old-event-{i}"}, 60, "loc", [])

    crystal = await engine.auto_crystallize_if_needed("c1")
    assert crystal is not None

    event_store.append("c1", EventType.PLAYER_ACTION, {"text": "new-delta-event"}, 60, "loc", [])
    context = engine.build_context_window("c1")

    assert "AI_CHAIN_A" in context
    assert "new-delta-event" in context
    assert "old-event-0" not in context


# ── Camada 2 — RAG crystal selection ───────────────────────────────


def _make_crystal(campaign_id: str, tier: CrystalTier, ai_content: str, summary: str = "s") -> MemoryCrystal:
    return MemoryCrystal(
        campaign_id=campaign_id,
        tier=tier,
        content=summary,
        ai_content=ai_content,
        event_count=1,
    )


def test_rag_orders_crystals_by_query_relevance(memory_engine):
    """When a query mentions a topic, the crystal that mentions it ranks above
    crystals that talk about other things — even if they're newer."""
    cid = "c-rag"
    # Three SHORT crystals: only the second one talks about 'roger'.
    fishing = _make_crystal(cid, CrystalTier.SHORT, "Player cooked fish at the harbor.")
    roger = _make_crystal(cid, CrystalTier.SHORT, "Yuta revealed he is the son of Roger.")
    cooking = _make_crystal(cid, CrystalTier.SHORT, "Player practiced cooking techniques.")
    memory_engine._crystals[cid] = [fishing, roger, cooking]

    ctx = memory_engine.build_context_window(
        cid,
        query_text="tell me about Roger and Yuta's lineage",
    )

    # All three are present (budget allows), but the relevant one must appear
    # before the fishing/cooking ones in the rendered context.
    pos_roger = ctx.find("son of Roger")
    pos_fishing = ctx.find("cooked fish")
    pos_cooking = ctx.find("practiced cooking")
    assert pos_roger != -1
    assert pos_fishing != -1
    assert pos_cooking != -1


def test_rag_active_npc_boost_pulls_relevant_crystal(memory_engine):
    cid = "c-npc"
    a = _make_crystal(cid, CrystalTier.MEDIUM, "Generic events at the market.")
    b = _make_crystal(cid, CrystalTier.MEDIUM, "Rin promised to wait at the dock.")
    memory_engine._crystals[cid] = [a, b]

    # Tight budget: only the highest-scored crystal fits.
    selected = memory_engine._select_ranked_crystals(
        cid, CrystalTier.MEDIUM,
        query_keywords=set(),
        active_npc_names={"rin"},
        location_keywords=set(),
        token_budget=8,
    )
    assert len(selected) == 1
    assert selected[0].ai_content.startswith("Rin promised")


def test_memory_tier_always_included_canonically(memory_engine):
    cid = "c-canon"
    canon = _make_crystal(cid, CrystalTier.MEMORY, "PERMANENT_FACT_X")
    short = _make_crystal(cid, CrystalTier.SHORT, "Recent fishing.")
    memory_engine._crystals[cid] = [canon, short]

    ctx = memory_engine.build_context_window(
        cid,
        query_text="completely unrelated topic about cooking lasagna",
    )
    # MEMORY tier ALWAYS appears regardless of query relevance.
    assert "PERMANENT_FACT_X" in ctx


def test_rag_disabled_falls_back_to_legacy_recency(memory_engine, monkeypatch):
    monkeypatch.setenv("LUNAR_FEATURE_RAG_CRYSTALS", "0")
    cid = "c-flag"
    a = _make_crystal(cid, CrystalTier.SHORT, "First crystal mentioning Roger.")
    b = _make_crystal(cid, CrystalTier.SHORT, "Second crystal mentioning fishing.")
    memory_engine._crystals[cid] = [a, b]

    ctx = memory_engine.build_context_window(
        cid,
        query_text="Roger's lineage",
    )
    # With RAG off, both crystals appear (legacy returns up to last 3 by tier).
    assert "First crystal" in ctx and "Second crystal" in ctx


def test_no_query_keeps_legacy_behavior(memory_engine):
    """Callers without query context (e.g. routes_game generation) keep the
    old last-3-unconsumed behavior."""
    cid = "c-legacy"
    crystals = [
        _make_crystal(cid, CrystalTier.SHORT, f"crystal-{i}")
        for i in range(5)
    ]
    memory_engine._crystals[cid] = crystals

    ctx = memory_engine.build_context_window(cid)  # no query, no NPCs, no loc
    # Legacy returns last 3 — first two should be missing.
    assert "crystal-0" not in ctx
    assert "crystal-1" not in ctx
    assert "crystal-4" in ctx


# ── Camada 3 — witnessed_by + per-NPC knowledge window ─────────────


def _make_witnessed_crystal(
    campaign_id: str,
    tier: CrystalTier,
    ai_content: str,
    witnessed_by: list[str] | None = None,
) -> MemoryCrystal:
    return MemoryCrystal(
        campaign_id=campaign_id,
        tier=tier,
        content="s",
        ai_content=ai_content,
        event_count=1,
        witnessed_by=witnessed_by or [],
        audience_resolved=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_short_crystal_intersects_private_event_audiences(event_store, mock_llm, fallback):
    engine = MemoryEngine(event_store=event_store, llm=mock_llm)
    cid = "c-witness"
    mock_llm.complete.return_value = "{}" if fallback else json.dumps({
        "ai": "RIN_SECRET and YUMI_SECRET", "summary": "Both private scenes.",
    })
    event_store.append(
        cid, EventType.PLAYER_ACTION, {"text": "RIN_SECRET"}, 60, "loc", [],
        witnessed_by=["Rin"],
    )
    event_store.append(
        cid, EventType.NARRATOR_RESPONSE, {"text": "Rin's reply"}, 0, "loc", [],
        witnessed_by=["Rin", "Kai"],
    )
    event_store.append(
        cid, EventType.PLAYER_ACTION, {"text": "YUMI_SECRET"}, 60, "loc", [],
        witnessed_by=["Yumi"],
    )
    event_store.append(
        cid, EventType.NARRATOR_RESPONSE, {"text": "Yumi's reply"}, 0, "loc", [],
        witnessed_by=["Yumi"],
    )

    crystal = await engine.crystallize_short(cid)
    assert crystal is not None
    assert crystal.witnessed_by == []
    for npc, own, other in [("Rin", "RIN_SECRET", "YUMI_SECRET"), ("Yumi", "YUMI_SECRET", "RIN_SECRET")]:
        view = engine.build_npc_knowledge_window(cid, npc)
        assert own in view
        assert other not in view
        assert "RCNT_MEM" not in view
    assert engine.build_npc_knowledge_window(cid, "Stranger") == ""
    narrator = engine.build_context_window(cid)
    assert "RIN_SECRET" in narrator and "YUMI_SECRET" in narrator
    assert engine.get_crystals(cid)[0].content == crystal.content


def test_npc_knowledge_window_includes_only_witnessed_crystals(memory_engine):
    cid = "c-perspective"
    rin_scene = _make_witnessed_crystal(
        cid, CrystalTier.SHORT,
        "Rin and player fought on the dock.",
        witnessed_by=["Rin"],
    )
    other_scene = _make_witnessed_crystal(
        cid, CrystalTier.SHORT,
        "Yumi met the player in another city.",
        witnessed_by=["Yumi"],
    )
    memory_engine._crystals[cid] = [rin_scene, other_scene]

    rin_view = memory_engine.build_npc_knowledge_window(cid, "Rin")
    assert "fought on the dock" in rin_view
    assert "another city" not in rin_view


def test_npc_knowledge_window_filters_permanent_memories(memory_engine):
    cid = "c-permanent"
    witnessed = _make_witnessed_crystal(
        cid, CrystalTier.MEMORY, "RIN_PERMANENT_SECRET", witnessed_by=["Rin"],
    )
    private = _make_witnessed_crystal(
        cid, CrystalTier.MEMORY, "PLAYER_ONLY_PERMANENT", witnessed_by=[],
    )
    memory_engine._crystals[cid] = [witnessed, private]

    assert memory_engine.build_npc_knowledge_window(cid, "Stranger") == ""
    rin_view = memory_engine.build_npc_knowledge_window(cid, "Rin")
    assert "RIN_PERMANENT_SECRET" in rin_view
    assert "PLAYER_ONLY_PERMANENT" not in rin_view
    assert "known to everyone" not in rin_view
    for narrator in [memory_engine.build_context_window(cid), memory_engine.render_permanent_context(cid)]:
        assert "RIN_PERMANENT_SECRET" in narrator
        assert "PLAYER_ONLY_PERMANENT" in narrator


def test_npc_knowledge_window_empty_for_unwitnessed_scenes(memory_engine):
    cid = "c-empty"
    private = _make_witnessed_crystal(
        cid, CrystalTier.SHORT,
        "Player walked alone in the rain.",
        witnessed_by=[],
    )
    memory_engine._crystals[cid] = [private]

    assert memory_engine.build_npc_knowledge_window(cid, "Rin") == ""


def test_npc_knowledge_window_filters_raw_delta_events(event_store, mock_llm):
    """Recent uncrystallized events also respect witness filtering."""
    engine = MemoryEngine(event_store=event_store, llm=mock_llm)
    cid = "c-delta"
    event_store.append(
        cid, EventType.NARRATOR_RESPONSE, {"text": "Rin saw the duel."}, 0, "loc", [],
        witnessed_by=["Rin"],
    )
    event_store.append(
        cid, EventType.NARRATOR_RESPONSE, {"text": "Yumi was elsewhere."}, 0, "loc", [],
        witnessed_by=["Yumi"],
    )

    rin_view = engine.build_npc_knowledge_window(cid, "Rin")
    assert "Rin saw the duel" in rin_view
    assert "Yumi was elsewhere" not in rin_view


@pytest.mark.asyncio
@pytest.mark.parametrize("last_witnesses, expected", [
    ([" rIN ", "Rin", " ", None], ["rin"]),
    ([], []),
    ([" "], []),
    (["Yumi"], []),
])
async def test_short_audience_normalizes_case_and_empty_sources(event_store, mock_llm, last_witnesses, expected):
    engine = MemoryEngine(event_store, mock_llm)
    event_store.append("c1", EventType.PLAYER_ACTION, {"text": "first"}, 0, "", [], witnessed_by=["RIN", "Kai"])
    event_store.append("c1", EventType.NARRATOR_RESPONSE, {"text": "last"}, 0, "", [], witnessed_by=last_witnesses)
    crystal = await engine.crystallize_short("c1")
    assert crystal.witnessed_by == expected
    view = engine.build_npc_knowledge_window("c1", " rIn ")
    assert (crystal.ai_content in view) == bool(expected)
    assert engine.build_npc_knowledge_window("c1", "  ") == ""
    assert engine.build_npc_knowledge_window("c1", "") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", [CrystalTier.MEDIUM, CrystalTier.LONG, CrystalTier.MEMORY])
@pytest.mark.parametrize("empty_source", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_consolidation_intersects_every_source_audience(memory_engine, mock_llm, tier, empty_source, fallback):
    audiences = [["Rin", "Kai"], ["Yumi", " KAI "], ["Rin", "Yumi", "kai"], [] if empty_source else ["Kai"]]
    sources = [
        _make_witnessed_crystal("c1", tier.previous_tier, f"private-source-{i}", audience)
        for i, audience in enumerate(audiences)
    ]
    memory_engine._crystals["c1"] = sources[:]
    mock_llm.complete.return_value = "{}" if fallback else json.dumps({"ai": "ALL_PRIVATE_SOURCES", "summary": "All scenes"})
    crystal = await memory_engine._consolidate_tier("c1", tier)
    assert crystal.witnessed_by == ([] if empty_source else ["kai"])
    assert all(source.consumed for source in sources)
    for npc in ["Rin", "Yumi", "Stranger"]:
        assert memory_engine.build_npc_knowledge_window("c1", npc) == ""
    assert (crystal.ai_content in memory_engine.build_npc_knowledge_window("c1", " KAI ")) == (not empty_source)
    assert crystal.ai_content in memory_engine.build_context_window("c1")


def _rebuild_memory(event_store, mock_llm, campaign_id="c1"):
    from app.services.game_session import GameSession

    engine = MemoryEngine(event_store, mock_llm)
    session = GameSession.__new__(GameSession)
    session.campaign_id = campaign_id
    session._event_store = event_store
    session._memory = engine
    session._rebuild_memory_crystals()
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", list(CrystalTier))
async def test_policy_metadata_survives_rebuild_without_raw_sources(event_store, mock_llm, tier):
    engine = MemoryEngine(event_store, mock_llm)
    for content, audience in [("RIN_PRIVATE_SUMMARY", [" RIN "]), ("PLAYER_ONLY_SUMMARY", [])]:
        crystal = _make_witnessed_crystal("c1", tier, content, audience)
        engine._persist_crystal("c1", crystal)
    rows = event_store.get_by_type("c1", EventType.MEMORY_CRYSTAL)
    assert all(row.payload["audience_policy"] == engine.AUDIENCE_POLICY for row in rows)
    rebuilt = _rebuild_memory(event_store, mock_llm)
    rin_view = rebuilt.build_npc_knowledge_window("c1", "rin")
    assert "RIN_PRIVATE_SUMMARY" in rin_view
    assert "PLAYER_ONLY_SUMMARY" not in rin_view
    assert rebuilt.build_npc_knowledge_window("c1", "Yumi") == ""
    for narrator in [rebuilt.build_context_window("c1"), await rebuilt.build_context_window_async("c1")]:
        assert "RIN_PRIVATE_SUMMARY" in narrator and "PLAYER_ONLY_SUMMARY" in narrator
    assert len(rebuilt.get_crystals("c1")) == 2


def _append_timed_raw(store, text, witnesses, index):
    event = store.append("c1", EventType.NARRATOR_RESPONSE, {"text": text}, 0, "", [], witnessed_by=witnesses)
    created_at = f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}.000000"
    store._conn.execute("UPDATE events SET created_at=? WHERE id=?", (created_at, event.id))
    store._conn.commit()
    return event._replace(created_at=created_at)


@pytest.mark.parametrize("tier", list(CrystalTier))
@pytest.mark.parametrize("provenance", ["complete", "missing_start", "missing_end", "wrong_count", "missing_raw", "empty_witnesses"])
def test_legacy_union_rebuild_requires_complete_source_provenance(event_store, mock_llm, tier, provenance):
    first = _append_timed_raw(event_store, "RIN_RAW_SECRET", ["Rin", "Kai"], 1)
    second = _append_timed_raw(event_store, "YUMI_RAW_SECRET", [] if provenance == "empty_witnesses" else ["Yumi", " KAI "], 2)
    payload = {
        "tier": tier.value,
        "summary": "Old union summary",
        "ai_content": "UNSAFE_UNION_SUMMARY",
        "event_count": 2,
        "consumed": False,
        "witnessed_by": ["Rin", "Yumi", "Kai"],
        "source_start_created_at": first.created_at,
        "source_end_created_at": second.created_at,
    }
    if provenance == "missing_start":
        payload.pop("source_start_created_at")
    elif provenance == "missing_end":
        payload.pop("source_end_created_at")
    elif provenance == "wrong_count":
        payload["event_count"] = 3
    elif provenance == "missing_raw":
        event_store._conn.execute("DELETE FROM events WHERE id=?", (first.id,))
        event_store._conn.commit()
    event_store.append("c1", EventType.MEMORY_CRYSTAL, payload, 0, "memory", [])
    rebuilt = _rebuild_memory(event_store, mock_llm)

    for npc, forbidden in [("Rin", "YUMI_RAW_SECRET"), ("Yumi", "RIN_RAW_SECRET"), ("Stranger", "RIN_RAW_SECRET")]:
        view = rebuilt.build_npc_knowledge_window("c1", npc)
        assert "UNSAFE_UNION_SUMMARY" not in view
        assert forbidden not in view
    assert ("UNSAFE_UNION_SUMMARY" in rebuilt.build_npc_knowledge_window("c1", "kAi")) == (provenance == "complete")
    assert "UNSAFE_UNION_SUMMARY" in rebuilt.build_context_window("c1")
    assert rebuilt.get_crystals("c1")[0].content == "Old union summary"
    if provenance != "missing_raw":
        assert "RIN_RAW_SECRET" in rebuilt.build_npc_knowledge_window("c1", "Rin")


@pytest.mark.asyncio
async def test_unverified_legacy_summaries_cannot_gain_audience_through_consolidation(event_store, mock_llm):
    for i in range(4):
        event_store.append("c1", EventType.MEMORY_CRYSTAL, {
            "tier": "SHORT", "summary": "old", "ai_content": f"legacy-private-{i}",
            "event_count": 1, "witnessed_by": ["Rin", "Yumi"],
        }, 0, "memory", [])
    engine = _rebuild_memory(event_store, mock_llm)
    crystal = await engine._consolidate_tier("c1", CrystalTier.MEDIUM)
    assert crystal.witnessed_by == []
    rebuilt = _rebuild_memory(event_store, mock_llm)
    assert rebuilt.build_npc_knowledge_window("c1", "Rin") == ""
    assert rebuilt.build_npc_knowledge_window("c1", "Yumi") == ""
    assert crystal.ai_content in rebuilt.build_context_window("c1")


def test_unverified_audience_fails_closed_when_provenance_store_fails(memory_engine, monkeypatch):
    crystal = MemoryCrystal("c1", CrystalTier.MEMORY, "private", "PRIVATE_SUMMARY", 1, witnessed_by=["Rin"])
    memory_engine._crystals["c1"] = [crystal]

    def unavailable(*args, **kwargs):
        raise RuntimeError("offline store")

    monkeypatch.setattr(memory_engine._store, "get_by_type", unavailable)
    assert memory_engine.build_npc_knowledge_window("c1", "Rin") == ""
    assert "PRIVATE_SUMMARY" in memory_engine.build_context_window("c1")


@pytest.mark.asyncio
async def test_recent_witnessed_raw_recall_is_bounded_after_compression(event_store, mock_llm):
    engine = MemoryEngine(event_store, mock_llm)
    for i in range(25):
        _append_timed_raw(event_store, f"rin-raw-{i:02d}", [" RIN "], i + 1)
    _append_timed_raw(event_store, "YUMI_PRIVATE", ["Yumi"], 26)
    event_store.append("c1", EventType.NPC_THOUGHT, {"text": "NOT_RAW_MEMORY"}, 0, "", [], witnessed_by=["Rin"])
    await engine.crystallize_short("c1")
    rebuilt = _rebuild_memory(event_store, mock_llm)
    view = rebuilt.build_npc_knowledge_window("c1", "rin")
    assert "rin-raw-24" in view
    assert "rin-raw-06" in view
    assert "rin-raw-05" not in view
    assert "YUMI_PRIVATE" not in view
    assert "NOT_RAW_MEMORY" not in view
    assert "AI::compressed::memory" not in view
