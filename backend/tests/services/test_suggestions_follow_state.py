"""Next-step hints follow the state, not the authored world.

The reported bug: the three buttons never changed with what the player did —
every turn offered a walk the rules would refuse and a conversation with
someone who had never been in the room. These tests pin the turn-by-turn
guarantees: locked exits are not destinations, a door that just opened becomes
one, the blocking key leads while it lies here, and only people in this room
are talked to.
"""

from __future__ import annotations

import pytest

from app.models.game import WorldDefinition
from app.services import suggestions as sugg


def _world_data():
    """A blunt starter room: one locked exit, one key, one person elsewhere."""
    return {
        "world_id": "w1",
        "name": "站点",
        "start_location_id": "dock",
        "language": "zh",
        "locations": [
            {
                "id": "dock",
                "name": "对接舱",
                "exits": [{"target_id": "corridor", "label": "内舱", "door_id": "hatch"}],
                "item_ids": ["keycard"],
            },
            {
                "id": "corridor",
                "name": "环形走廊",
                "exits": [{"target_id": "dock", "label": "气闸"}],
            },
        ],
        "items": [{"id": "keycard", "name": "主管钥匙卡"}],
        "doors": [
            {
                "id": "hatch",
                "name": "气闸内门",
                "location_id": "dock",
                "locked": True,
                "opened": False,
                "key_item_id": "keycard",
            }
        ],
        "entities": [
            {"id": "engineer", "name": "工程师艾拉", "location_id": "corridor"}
        ],
    }


def _world():
    return WorldDefinition.model_validate(_world_data())


def _actions(hints):
    return [h["action"] or h["speech"] for h in hints]


def test_a_locked_exit_is_never_offered_as_a_destination():
    hints = sugg.hints_from_state(
        location_id="dock", inventory=[], world=_world(), language="zh"
    )
    assert len(hints) == 3
    assert not any("走向内舱" in action for action in _actions(hints))


def test_the_key_that_blocks_the_way_is_offered_first():
    hints = sugg.hints_from_state(
        location_id="dock",
        world=_world(),
        language="zh",
        item_locations={"keycard": "dock"},
    )
    actions = _actions(hints)
    assert hints[0]["action"] == "拿起主管钥匙卡"
    assert hints[0]["target_id"] == "keycard"
    assert "打开气闸内门" in actions


def test_a_carried_key_is_not_offered_again():
    hints = sugg.hints_from_state(
        location_id="dock", inventory=["keycard"], world=_world(), language="zh"
    )
    assert "拿起主管钥匙卡" not in _actions(hints)
    # ...and the door it opens is now the move that matters.
    assert hints[0]["action"] == "打开气闸内门"


def test_an_opened_door_makes_the_walk_a_real_choice():
    hints = sugg.hints_from_state(
        location_id="dock",
        inventory=["keycard"],
        world=_world(),
        language="zh",
        doors={"hatch": True},
    )
    actions = _actions(hints)
    assert any("走向内舱" in action for action in actions)
    assert "打开气闸内门" not in actions
    # The engineer is still in the other room: nobody here to talk to.
    assert not any(h["speech"] for h in hints)


def test_only_people_in_the_room_are_addressed():
    hints = sugg.hints_from_state(
        location_id="corridor", inventory=["keycard"], world=_world(), language="zh"
    )
    targets = [h["target"] for h in hints if h["speech"]]
    assert targets and all(t == "工程师艾拉" for t in targets)


def test_a_person_who_walked_off_is_dropped():
    # The authored world puts the engineer in the corridor; the live state has
    # moved her to the dock, so the corridor screen must not offer her.
    hints = sugg.hints_from_state(
        location_id="corridor",
        inventory=[],
        world=_world(),
        language="zh",
        entities={"engineer": {"location_id": "dock"}},
    )
    assert not any("工程师艾拉" in action for action in _actions(hints))
    # The same live record puts her on the dock screen instead.
    hints = sugg.hints_from_state(
        location_id="dock",
        inventory=[],
        world=_world(),
        language="zh",
        entities={"engineer": {"location_id": "dock"}},
    )
    assert any("工程师艾拉" in action for action in _actions(hints))


def test_a_turn_result_reads_the_state_it_just_committed():
    result = {
        "state": {
            "location_id": "dock",
            "inventory": {"keycard": 1},
            "doors": {"hatch": True},
            "item_locations": {"keycard": "dock"},
            # She is still in the corridor; the caller's cast list must not
            # smuggle her onto this screen.
            "entities": {"engineer": {"location_id": "corridor"}},
        },
        "world": _world(),
    }
    hints = sugg.suggestions_for_result(
        result, characters=[("工程师艾拉", "engineer")], language="zh"
    )
    actions = _actions(hints)
    # Door open, key in hand: the walk is the first thing worth doing, and the
    # stale item location must not resurrect "pick up the key".
    assert any("走向内舱" in action for action in actions)
    assert "拿起主管钥匙卡" not in actions
    assert not any("工程师艾拉" in action for action in actions)


def test_a_turn_result_without_a_world_keeps_three_buttons():
    hints = sugg.suggestions_for_result({"state": {"location_id": "nowhere"}})
    assert len(hints) == 3
    assert all(h["action"] or h["speech"] for h in hints)


def test_the_sample_starter_campaign_reports_a_first_step():
    """The exact world from the bug report: a locked hatch and a key at hand."""
    world = _world()
    hints = sugg.hints_from_state(
        location_id="dock",
        inventory=[],
        world=world,
        language="zh",
        doors={"dock_hatch": False},
        item_locations={"keycard": "dock"},
    )
    assert hints[0]["action"] in {"拿起主管钥匙卡", "打开气闸内门"}
