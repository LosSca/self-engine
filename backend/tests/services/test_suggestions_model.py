"""Model-written options must never become a button the rules refuse."""

import asyncio
import json

import pytest

from app.services.suggestions import allowed_targets
from app.services.suggestions_model import (
    check_option,
    guidance_prompt,
    merge_options,
    model_suggestions,
    parse_options,
)


def _guidance(**overrides):
    base = {
        "location": "驾驶舱",
        "exits": ["货舱", "休息室"],
        "doors": ["气闸门"],
        "keys": [],
        "here_items": ["手电筒"],
        "characters": ["工程师艾拉"],
        "hostiles": [],
        "held": [],
        "quest": [],
        "in_combat": False,
        "language": "zh",
    }
    base.update(overrides)
    return base


def test_walk_to_a_real_exit_is_kept_without_duplicate_chip():
    option = check_option({"action": "走向货舱"}, allowed_targets(_guidance()))
    assert option == {"action": "走向货舱", "speech": "", "target": ""}


def test_invented_destination_is_rejected():
    assert check_option({"action": "走向灵魂海"}, allowed_targets(_guidance())) is None


def test_unknown_verb_is_rejected():
    assert check_option({"action": "生成一把宝剑"}, allowed_targets(_guidance())) is None


def test_required_target_without_a_known_name_is_rejected():
    assert check_option({"action": "拿起"}, allowed_targets(_guidance())) is None


def test_attack_needs_a_hostile_that_is_actually_here():
    targets = allowed_targets(_guidance())
    assert check_option({"action": "攻击工程师艾拉"}, targets) is not None
    assert check_option({"action": "攻击岩石巨人"}, targets) is None


def test_english_action_identifier_is_rejected():
    targets = allowed_targets(_guidance())
    assert check_option({"action": "investigate"}, targets) is None
    assert check_option({"action": "look"}, targets) is None


def test_speech_must_address_someone_in_the_room():
    targets = allowed_targets(_guidance())
    kept = check_option({"speech": "主引擎怎么了？", "target": "工程师艾拉"}, targets)
    assert kept == {"action": "", "speech": "主引擎怎么了？", "target": "工程师艾拉"}
    assert check_option({"speech": "你好", "target": "陌生人"}, targets) is None


def test_parse_options_keeps_only_valid_items_in_a_fenced_reply():
    reply = "```json\n" + json.dumps([
        {"action": "走向货舱"},
        {"action": "走向灵魂海"},
        {"action": "拿起手电筒"},
    ], ensure_ascii=False) + "\n```"
    kept = parse_options(reply, allowed_targets(_guidance()))
    assert [item["action"] for item in kept] == ["走向货舱", "拿起手电筒"]


def test_parse_options_tolerates_prose_and_garbage():
    targets = allowed_targets(_guidance())
    assert parse_options("", targets) == []
    assert parse_options("我不能这样做。", targets) == []
    assert parse_options("[not json]", targets) == []


def test_merge_fills_the_gap_from_the_rule_options():
    rules = [
        {"action": "观察四周", "speech": "", "target": ""},
        {"action": "检查驾驶舱", "speech": "", "target": ""},
        {"action": "等待", "speech": "", "target": ""},
    ]
    merged = merge_options([{"action": "走向货舱", "speech": "", "target": ""}], rules)
    assert [item["action"] for item in merged] == ["走向货舱", "观察四周", "检查驾驶舱"]


def test_guidance_prompt_lists_the_room_and_the_verbs():
    prompt = guidance_prompt(_guidance())
    assert "货舱" in prompt and "工程师艾拉" in prompt
    assert "move=前往/移动到/走到" in prompt
    assert "JSON" in prompt


class _StubRouter:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return self.reply


def test_model_suggestions_uses_the_model_options_first():
    router = _StubRouter(reply=json.dumps([
        {"action": "走向货舱"},
        {"action": "拿起手电筒"},
        {"speech": "你在修什么？", "target": "工程师艾拉"},
    ], ensure_ascii=False))
    rules = [{"action": "观察四周", "speech": "", "target": ""}]
    kept = asyncio.run(model_suggestions(router, _guidance(), fallback=rules))
    assert [item["action"] or item["speech"] for item in kept] == [
        "走向货舱", "拿起手电筒", "你在修什么？",
    ]
    assert router.calls[0][1]["reasoning"] is False


def test_model_suggestions_falls_back_when_the_model_fails():
    rules = [{"action": "观察四周", "speech": "", "target": ""}]
    kept = asyncio.run(model_suggestions(_StubRouter(error=RuntimeError("boom")), _guidance(), fallback=rules))
    assert [item["action"] for item in kept] == ["观察四周"]


def test_model_suggestions_falls_back_when_every_option_is_invalid():
    rules = [{"action": "观察四周", "speech": "", "target": ""}]
    router = _StubRouter(reply=json.dumps([{"action": "飞到月球"}], ensure_ascii=False))
    kept = asyncio.run(model_suggestions(router, _guidance(), fallback=rules))
    assert [item["action"] for item in kept] == ["观察四周"]


def test_model_suggestions_without_a_router_returns_the_rules():
    rules = [{"action": "观察四周", "speech": "", "target": ""}]
    assert asyncio.run(model_suggestions(None, _guidance(), fallback=rules)) == rules
