"""Phase 2: NPC dossiers keep one NPC's data out of another NPC's context."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.engines.npc_dossier import dossier_for
from app.models.game import EntityState, GameState, WorldDefinition


def _world() -> WorldDefinition:
    return WorldDefinition.model_validate(
        {
            "world_id": "w1",
            "name": "Village",
            "scenario_id": "s1",
            "start_location_id": "square",
            "locations": [
                {"id": "square", "name": "Square", "description": "A stone square."},
                {"id": "shop", "name": "Shop", "description": "Cramped."},
            ],
            "items": [{"id": "bread", "name": "Bread"}],
            "entities": [
                {
                    "id": "priest",
                    "name": "Old Priest",
                    "kind": "npc",
                    "location_id": "square",
                    "description": "A patient old man.",
                    "dialogue": ["Bless you, child."],
                    "attributes": {
                        "personality": "calm; devout",
                        "background": "He married your parents.",
                        "motivation": "keep the reliquary safe",
                    },
                },
                {
                    "id": "merchant",
                    "name": "Merchant",
                    "kind": "npc",
                    "location_id": "shop",
                    "faction": "guild",
                    "shop_items": ["bread"],
                },
            ],
        }
    )


def _state() -> GameState:
    state = GameState(campaign_id="c1", world_id="w1", location_id="square")
    state.entities["priest"] = EntityState(entity_id="priest", location_id="square", hp=10, max_hp=10)
    state.entities["merchant"] = EntityState(
        entity_id="merchant", location_id="shop", hp=8, max_hp=8,
        relations={"player": -2}, alert=2,
    )
    return state


def _mind(**thoughts):
    return SimpleNamespace(
        thoughts={key: SimpleNamespace(value=value) for key, value in thoughts.items()}
    )


def test_dossier_reads_authored_identity():
    dossier = dossier_for(_world(), _state(), "priest")
    assert dossier is not None
    assert dossier.name == "Old Priest"
    assert dossier.personality == ["calm", "devout"]
    assert "He married your parents." in dossier.background
    assert dossier.motivation == ["keep the reliquary safe"]


def test_dossier_reads_runtime_state():
    dossier = dossier_for(_world(), _state(), "merchant")
    assert dossier.relationships == {"player": -2}
    assert dossier.current_status["alert"] == 2
    assert dossier.current_status["location"] == "shop"
    assert dossier.merchant is True


def test_unknown_entity_has_no_dossier():
    assert dossier_for(_world(), _state(), "nobody") is None


def test_secrets_are_per_npc_and_never_shared():
    """Acceptance scenario 4: the merchant must not learn the priest's secret."""
    world, state = _world(), _state()
    priest = dossier_for(world, state, "priest", mind=_mind(**{"secret:reliquary": "buried under the altar"}))
    merchant = dossier_for(world, state, "merchant", mind=_mind(mood="greedy"))
    assert priest.secrets == ["buried under the altar"]
    assert merchant.secrets == []
    assert "buried under the altar" not in merchant.knows
    assert "buried under the altar" not in str(merchant.to_dict())


def test_thoughts_feed_mood_goal_and_knowledge():
    dossier = dossier_for(
        _world(), _state(), "priest",
        mind=_mind(mood="wary", goal="protect the door", **{"knows:the way north": "it is flooded"}),
    )
    assert dossier.emotional_state == {"mood": "wary"}
    assert "protect the door" in dossier.goals
    assert "it is flooded" in dossier.knows


def test_dossier_lines_render_the_identity():
    dossier = dossier_for(_world(), _state(), "priest")
    joined = "\n".join(dossier.identity_lines())
    assert "Old Priest (npc)" in joined
    assert "Personality: calm; devout" in joined


def test_to_dict_is_json_safe():
    import json
    payload = dossier_for(_world(), _state(), "priest").to_dict()
    assert json.loads(json.dumps(payload))["entity_id"] == "priest"
