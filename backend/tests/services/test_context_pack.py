from __future__ import annotations

"""P4: Context Pack - 顺序、预算、去重、溯源。"""

import pytest

from app.engines.memory_engine import CrystalTier, MemoryCrystal
from app.models.memory import EvidenceType, FactStatus, MemoryFact
from app.services.context_pack import (
    MODE_NARRATOR,
    SECTION_ORDER,
    ContextItem,
    build_context_pack,
    dedupe_items,
    estimate_tokens,
)


def _fact(value="friend", status=FactStatus.ACTIVE, events=("e1",)):
    return MemoryFact(
        id="fact_1",
        campaign_id="c1",
        subject_id="Eileen",
        predicate="status",
        object_value=value,
        status=status,
        confidence=0.9,
        evidence_type=EvidenceType.ENGINE_STATE,
        source_event_ids=list(events),
    )


def _episode(text, memory_id="mem_1", importance=0.8, confidence=0.9):
    return MemoryCrystal(
        campaign_id="c1",
        tier=CrystalTier.SHORT,
        content=text,
        ai_content=text,
        event_count=2,
        consumed=False,
        source_start_created_at="2026-01-02T00:00:00",
        source_end_created_at="2026-01-02T01:00:00",
        memory_id=memory_id,
        importance=importance,
        confidence=confidence,
    )


def test_live_state_always_renders_above_memory():
    pack = build_context_pack(
        "c1",
        scene="Temple courtyard at dusk",
        current_state="Location: temple",
        facts=[_fact()],
        episodes=[_episode("Eileen and you drove off the raiders.")],
        npc_perspective="knows: Eileen status friend",
        token_budget=800,
    )
    rendered = pack.render()
    order = [rendered.index(f"=== {name.upper()} ===") for name in rendered_headers(rendered)]
    assert order == sorted(order)
    assert rendered.index("=== CURRENT_STATE ===") < rendered.index("=== RELEVANT_EPISODES ===")


def rendered_headers(rendered):
    names = []
    for line in rendered.splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            names.append(line[4:-4].lower())
    return names


def test_a_duplicate_episode_is_only_rendered_once():
    pack = build_context_pack(
        "c1",
        episodes=[_episode("Eileen and you drove off the raiders."),
                  _episode("Eileen and you drove off the raiders.", memory_id="mem_2")],
        token_budget=800,
    )
    assert dedupe_items(pack) == 1
    assert pack.render().count("drove off the raiders") == 1


def test_every_fact_line_carries_its_source():
    pack = build_context_pack("c1", facts=[_fact(events=("e1", "e2"))], token_budget=800)
    rendered = pack.render()
    assert "fact fact_1" in rendered
    item = pack.section("relevant_facts").items[0]
    assert item.origin_ids == ["e1", "e2"]


def test_an_unverified_fact_is_labelled_differently_from_an_active_one():
    active = build_context_pack("c1", facts=[_fact()], token_budget=800).render()
    pending = build_context_pack(
        "c1", facts=[_fact(status=FactStatus.CANDIDATE)], token_budget=800,
    ).render()
    assert "(fact) Eileen status friend" in active
    assert "(candidate) Eileen status friend" in pending

def test_the_token_budget_is_respected():
    long_episodes = [_episode("Eileen " * 200, memory_id=f"mem_{i}") for i in range(8)]
    pack = build_context_pack("c1", current_state="Location: temple", episodes=long_episodes, token_budget=200)
    pack.render()
    assert pack.used_tokens <= 200


def test_a_section_cannot_eat_the_whole_budget():
    long_episodes = [_episode("Eileen " * 200, memory_id=f"mem_{i}") for i in range(8)]
    pack = build_context_pack(
        "c1",
        current_state="Location: temple",
        facts=[_fact()],
        episodes=long_episodes,
        token_budget=400,
    )
    rendered = pack.render()
    # Facts must still be visible even though the episodes are enormous.
    assert "=== RELEVANT_FACTS ===" in rendered
    assert pack.section("relevant_episodes").dropped > 0


def test_a_shrinking_budget_drops_the_cheapest_sections_first():
    pack = build_context_pack(
        "c1",
        current_state="Location: temple",
        facts=[_fact()],
        episodes=[_episode("short scene")],
        token_budget=400,
    )
    generous = pack.render(budget=400)
    tight = pack.render(budget=24)
    assert "Location: temple" in generous
    # Under a 24-token budget the live state survives and the memory does not:
    # the pack never trades current truth for recollection.
    assert "Location: temple" in tight
    assert "short scene" not in tight
    assert estimate_tokens(tight) <= 24


def test_an_empty_pack_renders_nothing():
    pack = build_context_pack("c1")
    assert pack.render() == ""
    assert pack.ordered_sections() == []


def test_the_pack_serialises_for_inspection():
    pack = build_context_pack("c1", current_state="Turn: 3", facts=[_fact()], token_budget=800)
    pack.render()
    data = pack.as_dict()
    assert data["mode"] == MODE_NARRATOR
    assert data["sections"]["current_state"]["items"][0]["text"] == "Turn: 3"
    assert data["sections"]["relevant_facts"]["items"][0]["source"].startswith("fact")


def test_a_pack_does_not_decide_permissions():
    # The pack renders whatever it is given: filtering is the caller's job and
    # the perspective module's, so this layer cannot become a second place to
    # get it wrong.
    pack = build_context_pack("c1", facts=[_fact(value="secret errand")], token_budget=800)
    assert "secret errand" in pack.render()


def test_section_order_is_stable_and_declared():
    assert SECTION_ORDER[0] == "scene"
    assert SECTION_ORDER.index("current_state") < SECTION_ORDER.index("relevant_facts")
    assert SECTION_ORDER.index("relevant_episodes") < SECTION_ORDER.index("npc_perspective")