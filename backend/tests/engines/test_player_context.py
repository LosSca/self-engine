"""Phase 2: PlayerContextBuilder assembles layered, NPC-scoped context."""
from __future__ import annotations

import pytest

from app.engines.player_context import (
    MAX_CONTEXT_CHARS,
    ContextSection,
    PlayerContext,
    PlayerContextBuilder,
    build_player_context,
)
from app.models.game import (
    EntityState,
    GameState,
    QuestProgress,
    WorldDefinition,
)


def _world() -> WorldDefinition:
    return WorldDefinition.model_validate(
        {
            "world_id": "w1",
            "name": "Village",
            "scenario_id": "s1",
            "start_location_id": "square",
            "locations": [
                {"id": "square", "name": "Square", "description": "A stone square."},
                {"id": "forest", "name": "Forest", "description": "Tall pines."},
            ],
            "items": [
                {"id": "bread", "name": "Bread"},
                {"id": "key", "name": "Brass Key"},
            ],
            "entities": [
                {
                    "id": "priest",
                    "name": "Old Priest",
                    "kind": "npc",
                    "location_id": "square",
                    "description": "A patient old man.",
                }
            ],
            "quests": [
                {
                    "id": "q1",
                    "title": "Open the cellar",
                    "objectives": [
                        {"id": "o1", "kind": "flag", "target": "opened", "description": "open it"}
                    ],
                }
            ],
        }
    )


def _state() -> GameState:
    state = GameState(
        campaign_id="c1",
        world_id="w1",
        location_id="square",
        turn=7,
        inventory={"bread": 2},
        item_locations={"key": "square"},
        quests={"q1": QuestProgress(quest_id="q1", status="active", objectives={"o1": 0})},
    )
    state.entities["priest"] = EntityState(entity_id="priest", location_id="square", hp=10, max_hp=10)
    return state


def test_scene_and_player_layers_are_always_present():
    context = build_player_context(_world(), _state())
    keys = [section.key for section in context.sections]
    assert keys[:4] == ["scene", "present", "items_here", "player"]
    assert all(not section.optional for section in context.sections[:4])
    rendered = context.render()
    assert "Square: A stone square." in rendered
    assert "Turn 7" in rendered
    assert "Bread x2" in rendered


def test_npc_layers_only_appear_when_an_npc_is_addressed():
    without = build_player_context(_world(), _state())
    assert without.npc is None
    assert without.section("npc_identity") is None

    with_npc = build_player_context(_world(), _state(), npc_id="priest")
    assert with_npc.npc is not None
    assert with_npc.section("npc_identity") is not None
    assert "Old Priest" in with_npc.section("npc_identity").render()


def test_layers_are_labelled_by_evidence_kind():
    context = build_player_context(_world(), _state(), npc_id="priest")
    rendered = context.render()
    assert "[Scene | fact]" in rendered
    assert context.kinds()["scene"] == "fact"


def test_memory_provider_feeds_the_npc_history_layer():
    class Memory:
        def build_npc_knowledge_window(self, campaign_id, npc_name):
            assert campaign_id == "c1"
            assert npc_name == "Old Priest"
            return "=== PRMNT_MEM (witnessed) ===\n- you handed him the key"

    context = build_player_context(
        _world(), _state(), npc_id="priest", memory_provider=Memory()
    )
    section = context.section("npc_memory")
    assert section is not None
    assert section.kind == "history"
    assert "you handed him the key" in section.render()
    assert "===" not in section.render()


def test_memory_provider_failure_degrades_to_no_memory_layer():
    class Broken:
        def build_npc_knowledge_window(self, campaign_id, npc_name):
            raise RuntimeError("graph down")

    context = build_player_context(
        _world(), _state(), npc_id="priest", memory_provider=Broken()
    )
    assert context.section("npc_memory") is None
    assert "Square" in context.render()


def test_canon_blocks_become_an_optional_canon_layer():
    knowledge = [{"kind": "canon", "title": "The reliquary", "content": "Sealed by the priest."}]
    context = build_player_context(_world(), _state(), knowledge=knowledge)
    section = context.section("canon")
    assert section is not None and section.optional
    assert "Sealed by the priest." in section.render()


def test_history_blocks_can_be_switched_off():
    knowledge = [{"kind": "history", "title": "Earlier", "content": "You arrived at dusk."}]
    on = build_player_context(_world(), _state(), knowledge=knowledge)
    off = build_player_context(_world(), _state(), knowledge=knowledge, include_history=False)
    assert on.section("history") is not None
    assert off.section("history") is None


def test_budget_drops_optional_layers_last_but_keeps_facts():
    context = PlayerContext(campaign_id="c1")
    context.add("scene", "Scene", ["A stone square."])
    context.add("canon", "Canon", ["x" * 400], optional=True)
    context.add("history", "History", ["y" * 400], optional=True)
    rendered = context.render(budget=200)
    assert "A stone square." in rendered
    assert "y" * 400 not in rendered


def test_builder_defaults_are_bounded():
    assert MAX_CONTEXT_CHARS >= 1000
    builder = PlayerContextBuilder()
    assert builder.memory_provider is None


def test_unknown_npc_id_leaves_the_dossier_empty():
    context = build_player_context(_world(), _state(), npc_id="ghost")
    assert context.npc is None
    assert context.section("npc_identity") is None
