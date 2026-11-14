"""Next-step suggestions: the rule lane answers "what now?" without a model."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.suggestions import (
    build_suggestions,
    pick_target,
    suggestions_for_result,
    target_directive,
)


def _world():
    hall = SimpleNamespace(
        id="hall",
        name="大厅",
        exits=[SimpleNamespace(target_id="garden", label=""), SimpleNamespace(target_id="cellar", label="地窖门")],
    )
    garden = SimpleNamespace(id="garden", name="花园", exits=[])
    cellar = SimpleNamespace(id="cellar", name="地窖", exits=[])
    key = SimpleNamespace(id="brass_key", name="铜钥匙")
    return SimpleNamespace(locations=[hall, garden, cellar], items=[key])


def test_always_three_options_even_in_an_empty_world():
    picks = build_suggestions()
    assert len(picks) == 3
    assert all(pick["action"] or pick["speech"] for pick in picks)


def test_exits_and_cast_come_first():
    picks = build_suggestions(
        location="大厅",
        exits=["花园", "地窖门"],
        characters=["守门人"],
        character_ids=["npc_1"],
        items=["铜钥匙"],
    )
    assert len(picks) == 3
    assert picks[0]["action"] == "走向花园"
    assert picks[1]["target"] == "守门人"
    assert picks[1]["target_id"] == "npc_1"


def test_suggestions_for_result_reads_the_turn_world():
    result = {
        "state": {"location_id": "hall", "inventory": {"brass_key": 1}},
        "world": _world(),
    }
    picks = suggestions_for_result(result, characters=[("守门人", "npc_1")])
    assert len(picks) == 3
    assert any("花园" in pick["action"] for pick in picks)


def test_suggestions_for_result_falls_back_to_the_world_getter():
    result = {"state": {"location_id": "garden", "inventory": {}}}
    picks = suggestions_for_result(result, world_getter=_world)
    assert len(picks) == 3


def test_suggestions_survive_a_broken_world():
    picks = suggestions_for_result({"state": {"location_id": "nowhere"}, "world": object()})
    assert len(picks) == 3


def test_pick_target_prefers_the_explicit_mention():
    name, mentioned = pick_target("[SAY] 我看向守门人 @守门人", ["铜钥匙", "守门人"])
    assert name == "守门人"
    # The second element is the matched mention as it appeared in the text.
    assert mentioned == "@守门人"


def test_target_directive_names_the_person():
    assert "守门人" in target_directive("守门人", "zh")
