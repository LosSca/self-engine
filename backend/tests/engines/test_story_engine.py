"""Tests for deterministic storyline advancement (app/engines/story_engine.py)."""
import pytest

from app.engines.story_engine import (
    advance_storyline,
    storyline_status,
    validate_storyline,
)
from app.models.game import Condition, GameState
from app.models.knowledge import StoryLineDef, Storyline, StoryNode
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


def test_side_root_without_a_trigger_is_rejected():
    storyline = _storyline([
        StoryNode(id="main", title="Main"),
        StoryNode(id="side", title="Side", kind="side"),
    ])
    errors = validate_storyline(storyline)
    assert len(errors) == 1
    assert "side" in errors[0] and "必须至少有一个条件或依赖事件" in errors[0]


@pytest.mark.parametrize("trigger,state_update", [
    ({"conditions": [{"kind": "has_item", "target": "iron_key"}]}, {"inventory": {"iron_key": 1}}),
    ({"requires_event": "keeper_alert"}, {"fired_events": ["keeper_alert"]}),
])
def test_side_nodes_trigger_independently_and_progress_without_becoming_active(trigger, state_update):
    world = _world()
    storyline = _storyline([
        StoryNode(id="main", title="Main",
                  conditions=[Condition(kind="door_open", target="warehouse_door")]),
        StoryNode(id="side", title="Side", kind="side", **trigger),
        StoryNode(id="followup", title="Side followup", kind="side", prereq=["side"]),
    ])
    assert validate_storyline(storyline, event_ids={"keeper_alert"}) == []
    state = _state()
    blocked = advance_storyline(storyline, state, world)
    assert blocked.entered == state.story_nodes == []
    assert blocked.pending == ["main", "side"]
    assert blocked.locked == ["followup"]
    assert "side" in blocked.reasons

    state = state.model_copy(update=state_update, deep=True)
    preview = storyline_status(storyline, state, world)
    assert state.story_nodes == []  # Read-only evaluation must not write history.
    status = advance_storyline(storyline, state, world)
    assert status == preview
    assert status.newly_entered == state.story_nodes == ["side", "followup"]
    assert status.active is None
    assert status.pending == ["main"]

    state.doors["warehouse_door"] = True
    resumed = advance_storyline(storyline, state, world)
    assert resumed.newly_entered == ["main"]
    assert resumed.active == "main"
    assert state.story_nodes == ["side", "followup", "main"]
    assert advance_storyline(storyline, state, world).newly_entered == []


@pytest.mark.parametrize("kind", ["main", "branch", "side"])
def test_disabled_lines_gate_new_nodes_but_preserve_history_when_reenabled(kind):
    world = _world()
    line = StoryLineDef(id="optional", title="Optional line", kind=kind, enabled=False)
    storyline = Storyline(lines=[line], nodes=[
        StoryNode(id="arrive", title="Arrival"),
        StoryNode(id="first", title="First", kind=kind, line="optional",
                  prereq=[] if kind == "side" else ["arrive"],
                  conditions=[Condition(kind="has_item", target="iron_key")]),
        StoryNode(id="second", title="Second", kind=kind, line="optional", prereq=["first"],
                  conditions=[Condition(kind="door_open", target="warehouse_door")]),
    ])
    assert validate_storyline(storyline) == []
    state = _state(inventory={"iron_key": 1})
    blocked = advance_storyline(storyline, state, world)
    assert state.story_nodes == ["arrive"]
    assert blocked.pending == ["first"]
    assert blocked.locked == ["second"]
    assert all("已停用" in blocked.reasons[node] for node in ["first", "second"])

    storyline.lines[0].enabled = True
    enabled = advance_storyline(storyline, state, world)
    assert enabled.newly_entered == ["first"]
    assert enabled.pending == ["second"]
    assert "条件未满足" in enabled.reasons["second"]

    storyline.lines[0].enabled = False
    state.doors["warehouse_door"] = True
    disabled_again = advance_storyline(storyline, state, world)
    assert disabled_again.newly_entered == []
    assert disabled_again.entered == state.story_nodes == ["arrive", "first"]
    assert disabled_again.active == ("first" if kind == "main" else "arrive")
    assert "first" not in disabled_again.reasons
    assert "已停用" in disabled_again.reasons["second"]

    storyline.lines[0].enabled = True
    resumed = advance_storyline(storyline, state, world)
    assert resumed.newly_entered == ["second"]
    assert state.story_nodes == ["arrive", "first", "second"]
    assert resumed.active == ("second" if kind == "main" else "arrive")
    assert advance_storyline(storyline, state, world).newly_entered == []


def test_unknown_line_reference_is_rejected_but_ungrouped_nodes_remain_valid():
    storyline = _storyline([StoryNode(id="main", title="Main", line="missing")])
    errors = validate_storyline(storyline)
    assert any("main" in error and "missing" in error and "不存在" in error for error in errors)
    storyline.nodes[0].line = ""
    assert validate_storyline(storyline) == []


@pytest.mark.parametrize("kind", ["branch", "side"])
def test_node_kind_must_match_its_line_kind(kind):
    storyline = Storyline(
        lines=[StoryLineDef(id="line", title="Line", kind=kind)],
        nodes=[StoryNode(id="main", title="Main", line="line")],
    )
    errors = validate_storyline(storyline)
    assert len(errors) == 1
    assert "main" in errors[0] and "line" in errors[0] and "不一致" in errors[0]


def test_duplicate_line_ids_are_rejected():
    storyline = Storyline(
        lines=[StoryLineDef(id="line", title="First"), StoryLineDef(id="line", title="Second")],
        nodes=[StoryNode(id="main", title="Main", line="line")],
    )
    assert validate_storyline(storyline) == ["剧情线 id 重复：'line'"]


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

# ------------------------------------------------------------- event packs
from app.engines.story_engine import pack_status  # noqa: E402
from app.models.knowledge import StoryEventPack  # noqa: E402


def _packed_storyline():
    return Storyline(
        name="主线",
        lines=[StoryLineDef(id="trunk", title="主干", kind="main")],
        packs=[
            StoryEventPack(
                id="pack_marquee",
                title="集市",
                event_ids=["ev_marquee"],
                requires=[Condition(kind="at_location", target="forest_edge")],
            ),
            StoryEventPack(
                id="pack_opening",
                title="开局",
                event_ids=["ev_opening"],
            ),
        ],
        nodes=[
            StoryNode(id="arrive", title="抵达", kind="main", line="trunk"),
            StoryNode(
                id="rumour",
                title="传闻",
                kind="side",
                requires_event_pack="pack_marquee",
            ),
        ],
    )


def test_pack_with_satisfied_conditions_is_armed():
    armed, fired = pack_status(
        _packed_storyline(),
        GameState(campaign_id="camp-1", world_id="demo_village", location_id="forest_edge"),
        _world(),
    )
    assert "pack_marquee" in armed
    assert fired == []


def test_pack_without_conditions_is_armed_at_the_opening():
    armed, _fired = pack_status(
        _packed_storyline(),
        GameState(campaign_id="camp-1", world_id="demo_village"),
        _world(),
    )
    assert "pack_opening" in armed


def test_pack_is_not_armed_when_its_condition_fails():
    armed, _fired = pack_status(
        _packed_storyline(),
        GameState(campaign_id="camp-1", world_id="demo_village", location_id="village_square"),
        _world(),
    )
    assert "pack_marquee" not in armed


def test_fired_once_pack_is_spent():
    state = GameState(
        campaign_id="camp-1",
        world_id="demo_village",
        fired_events=["ev_opening"],
    )
    armed, fired = pack_status(_packed_storyline(), state, _world())
    assert fired == ["pack_opening"]
    assert "pack_opening" not in armed


def test_repeatable_pack_stays_armed_after_firing():
    storyline = _packed_storyline()
    storyline.packs[1].once = False
    state = GameState(
        campaign_id="camp-1",
        world_id="demo_village",
        fired_events=["ev_opening"],
    )
    armed, fired = pack_status(storyline, state, _world())
    assert fired == ["pack_opening"]
    assert "pack_opening" in armed


def test_disabled_pack_is_ignored():
    storyline = _packed_storyline()
    storyline.packs[1].enabled = False
    armed, fired = pack_status(
        storyline, GameState(campaign_id="camp-1", world_id="demo_village"), _world()
    )
    assert "pack_opening" not in armed and "pack_opening" not in fired


def test_unknown_pack_reference_is_rejected():
    storyline = _packed_storyline()
    storyline.nodes[1].requires_event_pack = "pack_ghost"
    errors = validate_storyline(storyline, event_ids={"ev_marquee", "ev_opening"})
    assert any("pack_ghost" in error for error in errors)


def test_pack_referencing_unknown_event_is_rejected():
    storyline = _packed_storyline()
    storyline.packs[0].event_ids = ["ev_ghost"]
    errors = validate_storyline(storyline, event_ids={"ev_marquee", "ev_opening"})
    assert any("ev_ghost" in error for error in errors)


def test_empty_pack_is_rejected():
    storyline = _packed_storyline()
    storyline.packs[0].event_ids = []
    errors = validate_storyline(storyline)
    assert any("pack_marquee" in error for error in errors)


def test_duplicate_pack_ids_are_rejected():
    storyline = _packed_storyline()
    storyline.packs.append(StoryEventPack(id="pack_marquee", title="重复", event_ids=["ev_marquee"]))
    errors = validate_storyline(storyline)
    assert any("pack_marquee" in error for error in errors)


def test_side_node_waiting_on_a_pack_is_valid():
    errors = validate_storyline(_packed_storyline(), event_ids={"ev_marquee", "ev_opening"})
    assert errors == []


def test_side_node_without_any_anchor_is_rejected():
    storyline = Storyline(
        name="主线",
        nodes=[StoryNode(id="floating", title="悬浮", kind="side")],
    )
    errors = validate_storyline(storyline)
    assert any("floating" in error for error in errors)
