# -*- coding: utf-8 -*-
"""A listener who is named but standing somewhere else.

The player did nothing wrong: they named a real person. So the answer has to
be about that person's location, not about the room being empty.
"""
from __future__ import annotations

from app.engines.rules_engine import RulesEngine
from app.models.game import EntityDef, GameState, LocationDef, WorldDefinition
from app.services.text_action_service import TextActionParser


def _world() -> WorldDefinition:
    return WorldDefinition(
        world_id="w1",
        name="x",
        language="zh",
        start_location_id="corridor",
        locations=[
            LocationDef(id="corridor", name="环形走廊", npc_ids=[]),
            LocationDef(id="gallery", name="长廊", npc_ids=["traveler"]),
        ],
        entities=[
            EntityDef(
                id="traveler",
                name="旅人",
                location_id="gallery",
                dialogue=["观星台"],
            )
        ],
    )


def _state() -> GameState:
    return GameState(campaign_id="c1", world_id="w1", location_id="corridor")


def _parse(text: str):
    return TextActionParser().parse(text, _world(), _state())


def test_a_named_listener_who_left_still_resolves():
    parsed = _parse("[SAY] 向旅人开口，问这里发生了什么")
    assert parsed is not None
    assert parsed.action == "talk"
    assert parsed.target_id == "traveler"
    assert parsed.confidence == "exact"
    assert "somewhere else" in parsed.note


def test_the_rule_layer_says_where_they_are_instead_of_that_nobody_is_here():
    parsed = _parse("向旅人问好")
    assert parsed is not None and parsed.target_id == "traveler"

    outcome = RulesEngine()._talk(_world(), _state(), parsed.target_id, {})
    assert outcome.ok is False
    assert outcome.code == "entity_not_here"
    assert "旅人" in outcome.message
    assert "长廊" in outcome.message
    assert "nobody" not in outcome.message.lower()


def test_an_empty_room_with_no_name_still_says_nobody_is_here():
    parsed = _parse("[SAY] 向空气开口，问这里发生了什么")
    assert parsed is not None
    assert parsed.target_id is None
    assert parsed.confidence == "missing_target"

    outcome = RulesEngine()._talk(_world(), _state(), None, {})
    assert outcome.code == "nobody_here"


def test_a_listener_in_the_same_room_still_talks():
    state = _state()
    state.location_id = "gallery"
    parsed = TextActionParser().parse("向旅人问好", _world(), state)
    assert parsed is not None and parsed.target_id == "traveler"

    outcome = RulesEngine()._talk(_world(), state, parsed.target_id, {})
    assert outcome.ok is True
    assert outcome.code == "talked"


def test_an_absent_listener_does_not_leak_into_the_candidate_list():
    parsed = _parse("[SAY] 向旅人开口，问这里发生了什么")
    assert parsed is not None
    # The traveler is not a candidate here - they are not in the room - but the
    # target is still resolved, because the sentence named them.
    assert parsed.candidates == []
