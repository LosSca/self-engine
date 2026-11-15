from __future__ import annotations

"""P3: NPC 独立认知 - 世界真相 / 亲眼观察 / 个人推断 三分与权限前置过滤。"""

import pytest

from app.db.memory_fact_store import MemoryFactStore
from app.engines.memory_engine import CrystalTier, MemoryCrystal, MemoryEngine
from app.models.memory import EvidenceType, FactStatus, MemoryFact
from app.services.memory_perspective import (
    BELIEVES,
    KNOWS,
    Claim,
    build_perspective,
    facts_for_prompt,
    filter_facts_for_observer,
    perspective_fact_ids,
)


class _NullStore:
    """Minimum EventStore surface the engine touches during a context build."""

    def __init__(self, events=None):
        self.events = list(events or [])

    def append(self, **kwargs):
        return None

    def get_recent(self, campaign_id, limit=-1, **kwargs):
        return list(self.events)[:limit] if limit and limit > 0 else list(self.events)

    def get_by_type(self, campaign_id, event_type, limit=-1):
        return [e for e in self.events if e.event_type == event_type]

    def get_after(self, campaign_id, event_id=None, **kwargs):
        return []


@pytest.fixture()
def store(tmp_path):
    s = MemoryFactStore(str(tmp_path / "memory.db"))
    yield s
    s.close()


def _fact(store, subject, predicate, value, observers=(), status=FactStatus.ACTIVE):
    fact = MemoryFact(
        campaign_id="c1",
        subject_id=subject,
        predicate=predicate,
        object_value=value,
        observer_ids=list(observers),
        evidence_type=EvidenceType.ENGINE_STATE,
    )
    fact, _created = store.add_fact(fact)
    if status != FactStatus.CANDIDATE:
        store.advance_status(fact.id, status)
        fact = store.get_fact(fact.id)
    return fact

def test_a_secret_is_invisible_to_whoever_did_not_observe_it(store):
    secret = _fact(store, "Eileen", "mission", "secret errand", ["villain"])
    told = _fact(store, "Eileen", "status", "left the city", ["friend", "duke"])
    world = store.find_facts("c1")

    villain = [f.id for f in filter_facts_for_observer(world, "villain")]
    duke = [f.id for f in filter_facts_for_observer(world, "duke")]

    assert secret.id in villain and secret.id not in duke
    assert told.id in duke and told.id not in villain


def test_a_fact_without_observers_stays_public(store):
    public = _fact(store, "Eileen", "origin", "north")
    # Legacy facts written before observer tracking must not vanish.
    assert [f.id for f in filter_facts_for_observer(store.find_facts("c1"), "anyone")] == [public.id]


def test_an_unfiltered_list_cannot_be_rendered_for_a_specific_npc(store):
    secret = _fact(store, "Eileen", "mission", "secret errand", ["villain"])
    lines = facts_for_prompt(store.find_facts("c1"), observer="duke")
    assert all("secret errand" not in line for line in lines)
    assert secret.id


def test_a_perspective_keeps_a_wrong_belief_without_promoting_it(store):
    world = _fact(store, "Eileen", "status", "left the city", ["duke"])
    facts = filter_facts_for_observer(store.find_facts("c1"), "duke")
    duke = build_perspective(
        "duke",
        facts=facts,
        beliefs=[Claim("Eileen", "status", "dead", BELIEVES, 0.9)],
    )

    assert duke.mistaken_fact_ids == [world.id]
    rendered = "\n".join(facts_for_prompt(facts, observer="duke", perspective=duke))
    assert "heard, may be wrong" in rendered and "dead" in rendered
    # The world value is not asserted alongside the belief: one slot, one answer.
    assert "left the city" not in rendered


def test_a_belief_that_agrees_becomes_knowledge(store):
    _fact(store, "Eileen", "status", "left the city", ["duke"])
    facts = filter_facts_for_observer(store.find_facts("c1"), "duke")
    duke = build_perspective(
        "duke",
        facts=facts,
        beliefs=[Claim("Eileen", "status", "left the city", BELIEVES, 0.9)],
    )
    assert duke.claims[0].stance == KNOWS
    assert duke.mistaken_fact_ids == []


def test_two_npcs_do_not_share_memory(store):
    _fact(store, "Eileen", "status", "left the city", ["duke"])
    _fact(store, "Eileen", "mission", "secret errand", ["villain"])
    world = store.find_facts("c1")
    duke = build_perspective("duke", facts=filter_facts_for_observer(world, "duke"))
    villain = build_perspective("villain", facts=filter_facts_for_observer(world, "villain"))
    assert perspective_fact_ids(duke).isdisjoint(perspective_fact_ids(villain))

class _Graphiti:
    async def search(self, campaign_id, query, limit=8):
        return []


def _crystal(cid, content, witnesses):
    return MemoryCrystal(
        campaign_id=cid,
        tier=CrystalTier.SHORT,
        content=content,
        ai_content=content,
        event_count=1,
        consumed=False,
        source_start_created_at="2026-01-01T00:00:00",
        source_end_created_at="2026-01-01T00:00:00",
        witnessed_by=list(witnesses),
        audience_resolved=True,
    )


def test_the_npc_window_adds_only_the_facts_that_npc_may_know(store):
    _fact(store, "Eileen", "mission", "secret errand", ["villain"])
    _fact(store, "Eileen", "status", "left the city", ["duke"])
    engine = MemoryEngine(_NullStore(), llm=None, graphiti_engine=_Graphiti(), fact_store=store)
    engine._crystals["c1"] = [_crystal("c1", "The duke arrived at court.", ["duke"])]

    duke_perspective = engine.build_npc_perspective("c1", "duke")
    window = engine.build_npc_knowledge_window("c1", "duke", duke_perspective)
    assert "left the city" in window
    assert "secret errand" not in window

    villain_perspective = engine.build_npc_perspective("c1", "villain")
    villain_window = engine.build_npc_knowledge_window("c1", "villain", villain_perspective)
    assert "secret errand" in villain_window
    assert "left the city" not in villain_window
    assert "The duke arrived at court." not in villain_window


def test_the_window_without_a_perspective_is_unchanged(store):
    _fact(store, "Eileen", "status", "left the city", ["duke"])
    engine = MemoryEngine(_NullStore(), llm=None, fact_store=store)
    engine._crystals["c1"] = [_crystal("c1", "The duke arrived at court.", ["duke"])]
    assert "FACTS" not in engine.build_npc_knowledge_window("c1", "duke")
    assert "The duke arrived at court." in engine.build_npc_knowledge_window("c1", "duke")


def test_a_broken_fact_store_does_not_cost_the_npc_its_memories():
    class _Broken:
        def find_facts(self, campaign_id, **kwargs):
            raise RuntimeError("memory.db is gone")

    engine = MemoryEngine(_NullStore(), llm=None, fact_store=_Broken())
    engine._crystals["c1"] = [_crystal("c1", "Rin witnessed the duel.", ["rin"])]
    window = engine.build_npc_knowledge_window(
        "c1", "rin", engine.build_npc_perspective("c1", "rin"),
    )
    assert "Rin witnessed the duel." in window


def test_a_perspective_carries_the_events_that_npc_witnessed():
    from app.db.event_store import Event, EventType
    seen = Event(
        id="e1", campaign_id="c1", event_type=EventType.PLAYER_ACTION,
        payload={}, narrative_time_delta=0, location="gate", entities=[],
        created_at="2026-01-01T00:00:00", witnessed_by=["Rin"],
    )
    hidden = Event(
        id="e2", campaign_id="c1", event_type=EventType.PLAYER_ACTION,
        payload={}, narrative_time_delta=0, location="vault", entities=[],
        created_at="2026-01-01T00:01:00", witnessed_by=["Villain"],
    )
    engine = MemoryEngine(_NullStore([seen, hidden]), llm=None)
    perspective = engine.build_npc_perspective("c1", "rin")
    assert perspective.witnessed_event_ids == ["e1"]