"""Every memory says where it came from and how far to trust it."""
from __future__ import annotations

import pytest

from app.db.event_store import EventStore, EventType
from app.engines.memory_engine import (
    TIER_CONFIDENCE,
    CrystalTier,
    MemoryCrystal,
    MemoryEngine,
    derive_memory_id,
    stamp_provenance,
)


class FakeLLM:
    async def complete(self, messages, **kwargs):  # pragma: no cover - not used
        return "{}"


@pytest.fixture
def store(tmp_path):
    events = EventStore(str(tmp_path / "events.db"))
    yield events
    events.close()


@pytest.fixture
def engine(store):
    return MemoryEngine(event_store=store, llm=FakeLLM())


def _seed(store, campaign_id="camp-1", witnesses=("Aldo",)):
    # The audience of a crystal is the intersection of its sources, so both
    # events have to carry the witness.
    store.append(
        campaign_id, EventType.PLAYER_ACTION, {"text": "open the gate"}, 0, "square", ["Aldo"],
        witnessed_by=list(witnesses),
    )
    store.append(
        campaign_id, EventType.NARRATOR_RESPONSE, {"text": "The gate swings."}, 0, "square", [],
        witnessed_by=list(witnesses),
    )


def test_a_short_crystal_records_its_source_observer_and_entities(store, engine):
    _seed(store)
    import asyncio

    crystal = asyncio.run(engine.crystallize_short("camp-1"))
    assert crystal is not None
    assert crystal.memory_id.startswith("mem_")
    assert crystal.source == "events"
    assert crystal.observer == "player"
    assert crystal.confidence == TIER_CONFIDENCE["SHORT"]
    assert crystal.visibility == "npc"  # witnessed, so it belongs to an audience
    assert "Aldo" in crystal.related_entities
    assert crystal.status == "committed"


def test_a_witnessed_memory_is_labelled_not_silently_dropped(store, engine):
    _seed(store)
    import asyncio

    crystal = asyncio.run(engine.crystallize_short("camp-1"))
    label = crystal.provenance_label()
    assert "events" in label
    assert "Aldo" in label
    assert "verbatim" in label


def test_an_unwitnessed_memory_is_a_player_account(store, engine):
    _seed(store, witnesses=())
    import asyncio

    crystal = asyncio.run(engine.crystallize_short("camp-1"))
    assert crystal.visibility == "player"
    assert "player account" in crystal.provenance_label()


def test_consolidation_keeps_provenance_and_lowers_confidence(store, engine):
    import asyncio

    crystals = []
    for index in range(engine.CONSOLIDATION_COUNT):
        lower = MemoryCrystal(
            campaign_id="camp-1",
            tier=CrystalTier.SHORT,
            content=f"summary {index}",
            ai_content=f"ai {index}",
            event_count=2,
            source_start_created_at=f"2026-01-01T00:00:0{index}",
            source_end_created_at=f"2026-01-01T00:00:0{index}",
            witnessed_by=["Aldo"],
            audience_resolved=True,
        )
        stamp_provenance(lower, sources=[], source="events", importance=0.5)
        lower.related_entities = ["Aldo", f"place{index}"]
        crystals.append(lower)
    engine._crystals["camp-1"] = crystals

    merged = asyncio.run(engine._consolidate_tier("camp-1", CrystalTier.MEDIUM, language="en"))
    assert merged is not None
    assert merged.source == "consolidation"
    assert merged.observer == "player"
    assert merged.confidence < TIER_CONFIDENCE["MEDIUM"]
    assert merged.importance == 0.5
    assert merged.related_entities[:2] == ["Aldo", "place0"]


def test_provenance_survives_persistence(store, engine):
    _seed(store)
    import asyncio

    crystal = asyncio.run(engine.crystallize_short("camp-1"))
    rows = store.get_by_type("camp-1", EventType.MEMORY_CRYSTAL)
    payload = rows[-1].payload
    assert payload["memory_id"] == crystal.memory_id
    assert payload["source"] == "events"
    assert payload["confidence"] == crystal.confidence
    assert payload["related_entities"] == crystal.related_entities


def test_legacy_crystals_render_without_a_label():
    legacy = MemoryCrystal(
        campaign_id="camp-1",
        tier=CrystalTier.MEMORY,
        content="old",
        ai_content="old memory",
        event_count=3,
    )
    assert legacy.provenance_label() == ""


def test_a_memory_id_is_stable_for_the_same_span():
    first = derive_memory_id("camp-1", "SHORT", "a", "b")
    second = derive_memory_id("camp-1", "SHORT", "a", "b")
    assert first == second
    assert derive_memory_id("camp-2", "SHORT", "a", "b") != first
    assert len(first) == len("mem_") + 16


def test_permanent_context_carries_the_label(store, engine):
    crystal = MemoryCrystal(
        campaign_id="camp-1",
        tier=CrystalTier.MEMORY,
        content="the vault is real",
        ai_content="Vault exists.",
        event_count=8,
        witnessed_by=["Aldo"],
        audience_resolved=True,
    )
    stamp_provenance(crystal, source="consolidation")
    engine._crystals["camp-1"] = [crystal]
    rendered = engine.render_permanent_context("camp-1")
    assert "Vault exists." in rendered
    assert "consolidation" in rendered
    assert "Aldo" in rendered
