"""Tests for deterministic storyline advancement (app/engines/story_engine.py)."""
import pytest

from app.engines.story_engine import (
    advance_storyline,
    storyline_status,
    validate_storyline,
)
from app.models.game import Condition, GameState
from app.models.knowledge import Storyline, StoryNode
from tests.support import village_world


def _world():
    return village_world()


def _state(**kwargs):
    return GameState(campaign_id="camp-1", world_id="demo_village", **kwargs)


def _storyline(nodes):
    return Storyline(name="主线", nodes=nodes)


# ------------------------------------------------------------------ validation
def test_clean_storyline_validates():
    storyline = _storyline(
        [
            StoryNode(id="arrive", title="抵达"),
            StoryNode(id="open", title="开门", prereq=["arrive"]),
        ]
    )
    assert validate_storyline(storyline) == []


def test_duplicate_node_ids_are_rejected():
    storyline = _storyline(
        [StoryNode(id="a", title="一"), StoryNode(id="a", title="二")]
    )
    assert any("重复" in error or "出现多次" in error for error in validate_storyline(storyline))


def test_unknown_and_self_prerequisites_are_rejected():
    storyline = _storyline(
        [
            StoryNode(id="a", title="一", prereq=["a"]),
            StoryNode(id="b", title="二", prereq=["ghost"]),
        ]
    )
    errors = validate_storyline(storyline)
    assert any("以自身为前置" in e for e in errors)
    assert any("ghost" in e for e in errors)


def test_branch_requires_a_prerequisite_and_main_allows_only_one():
    storyline = _storyline(
        [
            StoryNode(id="main1", title="主干一"),
            StoryNode(id="main2", title="主干二", prereq=["main1"]),
            StoryNode(id="main3", title="主干三", prereq=["main1"]),
            StoryNode(id="branch", title="分支", kind="branch"),
        ]
    )
    errors = validate_storyline(storyline)
    assert any("分支" in e for e in errors)


def test_cycles_are_detected():
    storyline = _storyline(
        [
            StoryNode(id="a", title="一", prereq=["b"]),
            StoryNode(id="b", title="二", prereq=["a"]),
        ]
    )
    assert any("循环" in error for error in validate_storyline(storyline))


def test_unknown_knowledge_and_events_are_rejected_when_sets_are_given():
    storyline = _storyline(
        [
            StoryNode(
                id="a", title="一", knowledge_ids=["k-missing"], requires_event="e-missing"
            )
        ]
    )
    errors = validate_storyline(storyline, knowledge_ids=set(), event_ids=set())
    assert any("k-missing" in e for e in errors)
    assert any("e-missing" in e for e in errors)


# ------------------------------------------------------------------- evaluation
def test_satisfied_chain_catches_up_in_one_evaluation():
    """Progress is a pure function of state: a satisfied chain is entered at once."""
    world = _world()
    storyline = _storyline(
        [
            StoryNode(id="arrive", title="抵达"),
            StoryNode(id="open", title="开门", prereq=["arrive"]),
        ]
    )
    status = storyline_status(storyline, _state(), world)
    assert status.entered == ["arrive", "open"]
    assert status.active == "open"
    assert status.pending == []


def test_door_condition_blocks_a_prereq_satisfied_node():
    world = _world()
    storyline = _storyline(
        [
            StoryNode(id="arrive", title="抵达"),
            StoryNode(
                id="open",
                title="开门",
                prereq=["arrive"],
                conditions=[Condition(kind="door_open", target="warehouse_door")],
            ),
        ]
    )
    blocked = storyline_status(storyline, _state(), world)
    assert "open" in blocked.pending
    assert "条件未满足" in blocked.reasons["open"]

    unlocked = storyline_status(storyline, _state(doors={"warehouse_door": True}), world)
    assert unlocked.entered == ["arrive", "open"]
    assert unlocked.active == "open"


def test_requires_event_gates_a_node():
    world = _world()
    storyline = _storyline(
        [
            StoryNode(id="arrive", title="抵达"),
            StoryNode(id="alert", title="警报", requires_event="keeper_alert"),
        ]
    )
    blocked = storyline_status(storyline, _state(), world)
    assert "alert" not in blocked.entered

    fired = storyline_status(storyline, _state(fired_events=["keeper_alert"]), world)
    assert "alert" in fired.entered


def test_advance_writes_nodes_once_and_is_idempotent():
    world = _world()
    storyline = _storyline([StoryNode(id="arrive", title="抵达")])
    state = _state()

    first = advance_storyline(storyline, state, world)
    assert state.story_nodes == ["arrive"]
    assert first.newly_entered == ["arrive"]

    second = advance_storyline(storyline, state, world)
    assert state.story_nodes == ["arrive"]
    assert second.newly_entered == []


def test_branch_nodes_require_their_own_prereq():
    world = _world()
    storyline = _storyline(
        [
            StoryNode(id="arrive", title="抵达"),
            StoryNode(id="open", title="开门", prereq=["arrive"]),
            StoryNode(
                id="hidden", title="密道", kind="branch", prereq=["open"],
                conditions=[Condition(kind="door_open", target="warehouse_door")],
            ),
        ]
    )
    state = _state(story_nodes=["arrive", "open"], doors={"warehouse_door": True})
    status = advance_storyline(storyline, state, world)
    assert status.newly_entered == ["hidden"]
    assert status.active == "open"  # last entered main node