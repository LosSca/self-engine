# -*- coding: utf-8 -*-
"""聊天要能推进游戏：每一轮都必须有后果，建议必须跟着玩家的话走。

用户反馈（2026-11-15）："游戏根本不能由对话快速的有效推进"。根因不是
某一条规则写错，而是三处叠加——认不出的句子走 _improvise 且 effects=[]
（回显式假动作）、建议清单完全不看玩家说了什么、以及拒绝文案里夹着
"名字要和世界里现有的一致"这种开发规范。这三条现在各有一个用例守。
"""
from __future__ import annotations

import pytest

from app.engines.rules_engine import RulesEngine
from app.models.game import EntityDef, GameState, ItemDef, LocationDef, WorldDefinition
from app.services.rejection import available_actions


def _world(language: str = "zh", *, with_guard: bool = False) -> WorldDefinition:
    entities = [
        EntityDef(
            id="gate_watcher",
            name="守林人" if language == "zh" else "Gate Watcher",
            description="他靠在树上。",
            kind="npc",
            location_id="forest_edge",
        )
    ]
    if with_guard:
        entities.append(
            EntityDef(
                id="guard",
                name="守卫" if language == "zh" else "Guard",
                description="他握着一杆长矛。",
                kind="npc",
                location_id="square",
            )
        )
    return WorldDefinition(
        world_id="w",
        name="World",
        start_location_id="square",
        language=language,
        locations=[
            LocationDef(id="square", name="村口广场" if language == "zh" else "Square"),
            LocationDef(id="forest_edge", name="林边" if language == "zh" else "Forest Edge"),
        ],
        entities=entities,
    )


def _door():
    from app.models.game import DoorDef

    return DoorDef(
        id="door",
        name="仓库门",
        location_id="square",
        key_item_id="key",
        opened=False,
    )


def _square_with_door():
    from app.models.game import ExitDef

    return LocationDef(
        id="square",
        name="村口广场",
        exits=[ExitDef(label="仓库", target_id="forest_edge", door_id="door")],
    )


def _eng() -> RulesEngine:
    return RulesEngine()


def _state(location: str = "square") -> GameState:
    return GameState(campaign_id="c", world_id="w", location_id=location)


class TestImproviseHasConsequences:
    def test_an_attempt_is_recorded_with_its_own_words(self):
        outcome = _eng()._improvise(
            _world(), _state(), params={"attempt": "四处打听一下守卫的事"}
        )
        assert outcome.ok is True
        assert outcome.code == "improvised"
        assert "四处打听一下守卫的事" in outcome.message
        # It is quoted as the player wrote it, not re-worded by the engine.
        assert "「四处打听一下守卫的事」" in outcome.message

    def test_an_attempt_costs_time(self):
        outcome = _eng()._improvise(_world(), _state(), params={"attempt": "蹲下看看裂缝"})
        assert outcome.minutes and outcome.minutes > 0

    def test_someone_present_notices(self):
        world = _world(with_guard=True)
        outcome = _eng()._improvise(world, _state(), params={"attempt": "蹲下看看裂缝"})
        targets = [e.target for e in outcome.effects if e.kind == "adjust_alert"]
        assert "guard" in targets, "在场的人必须察觉，否则自由文本等于没做任何事"

    def test_an_empty_room_says_so_instead_of_pretending(self):
        outcome = _eng()._improvise(_world(), _state(), params={"attempt": "蹲下看看裂缝"})
        assert "没有别人看见" in outcome.message or "Nobody else" in outcome.message

    def test_english_world_answers_in_english(self):
        outcome = _eng()._improvise(
            _world("en"), _state(), params={"attempt": "check the cracks"}
        )
        assert "check the cracks" in outcome.message
        assert "你" not in outcome.message


class TestHintsFollowTheUtterance:
    def test_the_named_thing_leads(self):
        world = _world(with_guard=True)
        hints = available_actions(world, _state(), chinese=True, text="我想和守卫说话")
        assert hints and "守卫" in hints[0]

    def test_unrelated_sentences_get_different_advice(self):
        """A sentence naming someone must not get the room's generic advice.

        The generic list is the right fallback when nothing was named - it is
        what "look around" means. What was wrong before was that the *named*
        case also got it, so paying attention to the player changed nothing.
        """
        world = _world(with_guard=True)
        # A room with more than the person just named: otherwise "the room's
        # advice" and "the advice about the guard" are legitimately the same
        # sentence, and the comparison would prove nothing.
        world = world.model_copy(update={"items": [
            ItemDef(id="lamp", name="油灯" if True else "Lamp"),
        ]})
        state = _state()
        state.item_locations["lamp"] = "square"
        a = available_actions(world, state, chinese=True, text="我想和守卫说话")
        b = available_actions(world, state, chinese=True, text="随便说说")
        assert a and b
        assert a[0] == "对 守卫 说话", "先回应玩家点名的人"
        assert a != b, "点了名却拿到房间通用建议，等于没在听玩家说话"

    def test_nothing_named_falls_back_to_the_room(self):
        world = _world(with_guard=True)
        hints = available_actions(world, _state(), chinese=True, text="嗯")
        assert hints, "点了名却什么都认不出时，仍要给出房间里能做的事"

class TestStateChangesAreNarrated:
    """状态变了却不说，玩家只会看见"没什么发生"。"""

    def test_an_alert_change_is_spoken(self):
        from app.models.game import Effect
        from app.services.narrative_service import describe_effects

        world = _world(with_guard=True)
        lines = describe_effects(
            world, _state(), [Effect(kind="adjust_alert", target="guard", amount=1)]
        )
        assert lines, "adjust_alert 必须有对应句，否则 improvise 的效果对玩家不可见"
        assert any("守卫" in line for line in lines)

    def test_an_unknown_entity_produces_no_sentence(self):
        from app.models.game import Effect
        from app.services.narrative_service import describe_effects

        world = _world()
        lines = describe_effects(
            world, _state(), [Effect(kind="adjust_alert", target="ghost", amount=1)]
        )
        assert lines == []

class TestAnAttemptTurnsSomethingUp:
    """空房间里的一次尝试，必须换来信息，而不是"还是老样子"。"""

    def test_a_search_in_an_empty_room_reveals_a_real_lead(self):
        world = _world()
        world = world.model_copy(update={
            "items": [ItemDef(id="key", name="铁钥匙")],
            "doors": [_door()],
        })
        world = world.model_copy(update={"locations": [
            _square_with_door(),
            world.locations[1],
        ]})
        op = _eng()._improvise(
            world, _state(), params={"attempt": "四处打听一下这里的事"}
        )
        assert op.ok is True
        assert "打听出" in op.message, "空房间里的一次寻找不能只是换个说法说'什么都没发生'"
        assert "铁钥匙" in op.message

    def test_a_lead_names_the_key_that_opens_the_door(self):
        world = _world()
        world = world.model_copy(update={
            "items": [ItemDef(id="key", name="铁钥匙")],
            "doors": [_door()],
            "locations": [_square_with_door(), world.locations[1]],
        })
        op = _eng()._improvise(world, _state(), params={"attempt": "检查一下四周"})
        assert "仓库门" in op.message and "铁钥匙" in op.message

    def test_a_non_search_attempt_still_reveals_nothing_made_up(self):
        world = _world()
        op = _eng()._improvise(world, _state(), params={"attempt": "蹲下来盯着地面"})
        # No search marker, and this world has nothing to lead to: the honest
        # answer stays the old one. The engine may not invent a lead.
        assert "打听出" not in op.message