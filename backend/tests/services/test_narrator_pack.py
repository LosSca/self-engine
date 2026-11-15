"""The narrator's memory blocks must survive the switch to a pack.

The flag (MEMORY_CONTEXT_PACK) exists so the pack can be turned on without
changing a single thing the narrator already reads. These tests pin exactly
that contract: same headings, same content, live state ahead of recall, and a
budget that drops the *oldest recall* rather than the current board.
"""
from __future__ import annotations

import pytest

from app.services.context_pack import estimate_tokens
from app.services.narrator_pack import (
    build_narrator_pack,
    context_pack_enabled,
    pack_budget,
    pack_notes,
    render_narrator_pack,
)


class _Fact:
    def __init__(self, subject_id="Eileen", predicate="relationship", object_value="friend",
                 status="active", confidence=0.9, fact_id="f1", source_event_ids=("e1",)):
        self.id = fact_id
        self.subject_id = subject_id
        self.predicate = predicate
        self.object_value = object_value
        self.status = status
        self.confidence = confidence
        self.source_event_ids = list(source_event_ids)
        self.valid_from = "2026-02-01T00:00:00"


class TestTheFlag:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("MEMORY_CONTEXT_PACK", raising=False)
        assert context_pack_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_truthy_values_turn_it_on(self, monkeypatch, value):
        monkeypatch.setenv("MEMORY_CONTEXT_PACK", value)
        assert context_pack_enabled() is True

    @pytest.mark.parametrize("value", ["0", "", "off", "nope"])
    def test_anything_else_keeps_the_old_path(self, monkeypatch, value):
        monkeypatch.setenv("MEMORY_CONTEXT_PACK", value)
        assert context_pack_enabled() is False

    def test_an_explicit_budget_wins(self, monkeypatch):
        monkeypatch.setenv("MEMORY_CONTEXT_PACK_TOKENS", "1234")
        assert pack_budget(64000) == 1234

    def test_a_context_window_scales_the_budget_but_stays_bounded(self):
        assert pack_budget(64000) == 6000
        assert pack_budget(4000) >= 1200
        assert pack_budget(0) > 0


class TestRendering:
    def test_headings_are_the_ones_the_narrator_already_reads(self):
        pack = build_narrator_pack(
            "c1",
            npc_states="NPC STATES (what each NPC is currently thinking/feeling):\n- Eileen: calm",
            memory_context="The knight pulled you out of the barn.",
            inventory="torch x1",
        )
        blocks = render_narrator_pack(pack)
        assert blocks["state"].startswith("NPC STATES (what each NPC is currently thinking/feeling):")
        assert blocks["episodes"].startswith("WORLD MEMORY:")
        assert "knight pulled you out" in blocks["episodes"]

    def test_live_state_is_rendered_before_memory(self):
        pack = build_narrator_pack(
            "c1",
            npc_states="NPC STATES: the guard is following you",
            memory_context="A year ago the guard was asleep.",
        )
        rendered = pack.render()
        # Both sections were fed strings that already carry the narrator's own
        # heading, so the pack's section header is the tie-breaker here.
        assert rendered.index("CURRENT_STATE") < rendered.index("RELEVANT_EPISODES")

    def test_a_fact_says_what_it_is(self):
        pack = build_narrator_pack("c1", facts=[_Fact(), _Fact(fact_id="f2", status="candidate", confidence=0.4)])
        blocks = render_narrator_pack(pack)
        assert "(fact) Eileen relationship friend" in blocks["facts"]
        assert "(candidate) Eileen relationship friend" in blocks["facts"]

    def test_an_incomplete_fact_is_dropped_not_rendered_as_noise(self):
        pack = build_narrator_pack("c1", facts=[_Fact(subject_id="", object_value="")])
        assert "relevant_facts" not in render_narrator_pack(pack)["facts"]

    def test_nothing_at_all_renders_as_empty_strings(self):
        blocks = render_narrator_pack(build_narrator_pack("c1"))
        assert blocks["state"] == "" and blocks["episodes"] == "" and blocks["facts"] == ""

    def test_the_journal_joins_the_episode_section(self):
        pack = build_narrator_pack("c1", memory_context="crystal text", journal="STORY LOG: the gate fell")
        episodes = render_narrator_pack(pack)["episodes"]
        assert "crystal text" in episodes and "the gate fell" in episodes


class TestBudget:
    def test_used_tokens_stay_under_the_budget(self):
        memory = "\n".join(f"memory line {i}: something happened that day" for i in range(200))
        pack = build_narrator_pack("c1", memory_context=memory, token_budget=300)
        render_narrator_pack(pack)
        assert pack.used_tokens <= 300
        assert pack.notes

    def test_the_dropped_part_is_counted_not_silently_lost(self):
        memory = "\n".join(f"line {i} of a very long recap " + "x" * 80 for i in range(50))
        pack = build_narrator_pack("c1", memory_context=memory, token_budget=200)
        blocks = render_narrator_pack(pack)
        # Whatever the pack refused is named in the notes - either as a dropped
        # item or as a clipped one - so a review can see the prompt was cut.
        assert pack.notes
        assert estimate_tokens(blocks["episodes"]) <= 200

    def test_the_live_state_wins_when_the_budget_is_tight(self):
        # The pack orders current_state above episodes, and it is small; the
        # long recap is what must give way.
        memory = "\n".join("a very long recap sentence " * 20 for _ in range(40))
        pack = build_narrator_pack(
            "c1", npc_states="NPC STATES: the guard is here", memory_context=memory, token_budget=120
        )
        blocks = render_narrator_pack(pack)
        assert "guard is here" in blocks["state"]

    def test_the_rendered_text_respects_the_token_budget(self):
        # The pack counts tokens and render() takes characters; the blocks that
        # reach the prompt must follow the token budget, not four times it.
        memory = "\n".join(f"memory line {i}: something happened that day" for i in range(400))
        pack = build_narrator_pack("c1", memory_context=memory, token_budget=300)
        blocks = render_narrator_pack(pack)
        rendered = blocks["episodes"] + blocks["state"] + blocks["facts"]
        assert estimate_tokens(rendered) <= 300

    def test_notes_describe_the_fold(self):
        pack = build_narrator_pack("c1", memory_context="one line", token_budget=500)
        render_narrator_pack(pack)
        assert "context pack:" in pack_notes(pack)
        assert estimate_tokens("你好 world") > 0
