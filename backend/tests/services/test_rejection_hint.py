"""解析失败时玩家至少要看到「现在能做什么」。

文本链路此前在没听懂时返回空叙事（status="skipped"），前端只能显示
「（行动被规则拒绝）」。这不是规则错，是反馈缺失：玩家不知道现场有什么
可以动，于是觉得会话推进不下去。
"""

import pytest

from app.models.game import WorldDefinition
from app.services.rejection import available_actions, rejection_message
from tests.services.test_text_action_parser import service  # noqa: F401  (pytest fixture)
from tests.support import fresh_state, village_world_data


def _world_and_state():
    """The demo village, translated, so hints can be checked against real names."""
    data = village_world_data()
    for location in data["locations"]:
        if location["id"] == "village_square":
            location["name"] = "村庄广场"
        elif location["id"] == "forest_edge":
            location["name"] = "森林边缘"
    data["language"] = "zh"
    world = WorldDefinition.model_validate(data)
    return world, fresh_state(world)


def test_the_hint_names_a_real_exit():
    world, state = _world_and_state()
    options = available_actions(world, state, chinese=True)
    assert options, "一个地点有出口时，提示不能是空的"
    assert any("森林边缘" in option or "前往" in option for option in options)


def test_a_chinese_rejection_reads_as_sentences_not_codes():
    world, state = _world_and_state()
    message = rejection_message(world, state, text="我推开左边那扇门")
    assert "我推开左边那扇门" in message
    assert "可以试试" in message
    assert "missing_target" not in message


def test_an_english_world_gets_an_english_hint():
    world, state = _world_and_state()
    world.language = "en"
    message = rejection_message(world, state, text="push the left door")
    assert message.startswith("I could not match")
    assert "go to" in message


@pytest.mark.asyncio
async def test_the_text_lane_returns_the_hint_as_the_message(service):
    result = await service.execute("camp-1", "[CONTINUE]", narrate=False)
    assert result["status"] == "rejected"
    message = result["result"]["message"]
    # 以前这里是 "无法理解这条行动：…"，玩家读完仍不知道该做什么。
    assert "[CONTINUE]" in message
    assert "Try:" in message or "可以试试" in message
    assert "unparsed" not in message