"""Opening-screen hints: three options before the first turn exists."""

from __future__ import annotations

from app.services import suggestions as sugg


class _Stub:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _world():
    hall = _Stub(id="hall", name="前厅", exits=[_Stub(target_id="yard", label=""), _Stub(target_id="vault", label="侧门")])
    yard = _Stub(id="yard", name="院子", exits=[])
    vault = _Stub(id="vault", name="库房", exits=[])
    return _Stub(locations=[hall, yard, vault], items=[_Stub(id="key", name="铜钥匙")])


def test_hints_use_the_room_the_campaign_is_standing_in():
    hints = sugg.hints_from_state(
        location_id="hall",
        inventory=["key"],
        characters=[("老锁匠", "e1")],
        world=_world(),
    )
    assert len(hints) == 3
    joined = " ".join(h["action"] + h["speech"] for h in hints)
    assert "院子" in joined
    assert hints[1]["target"] == "老锁匠"


def test_hints_fall_back_to_the_start_location_before_the_first_turn():
    hints = sugg.hints_from_state(world=_world(), language="zh")
    assert len(hints) == 3
    # The first location stands in for the room, so its exit is a real choice.
    assert hints[0]["target"] == "院子"


def test_hints_without_a_world_still_offer_executable_actions():
    hints = sugg.hints_from_state(world=None)
    assert [h["action"] for h in hints] == ["看", "观察四周", "看看脚下"]


def test_a_bare_world_offers_three_different_acts():
    hints = sugg.hints_from_state(world=None)
    assert len({h["action"] for h in hints}) == 3


def test_every_fallback_is_an_action_the_rule_lane_resolves():
    hints = sugg.hints_from_state(world=None, language="en")
    assert hints[0]["action"] == "look"