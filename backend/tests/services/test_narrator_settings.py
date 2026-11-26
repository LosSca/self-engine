"""Panel settings reach the two model layers: narrative and options."""
import pytest

from app.db.prefs_store import get_prefs_store
from app.services.action_service import ActionService
from app.services.suggestions_model import guidance_prompt, model_suggestions


@pytest.fixture()
def set_prefs():
    def _set(**values):
        store = get_prefs_store()
        try:
            store.put(values)
        finally:
            store.close()

    return _set


def test_narrative_sampling_is_taken_from_the_panel(set_prefs):
    set_prefs(narrator_temperature=1.15, narrator_max_tokens=900, narrator_timeout=45.5)
    options = ActionService._apply_sampling_prefs(None)
    assert options == {"temperature": 1.15, "max_tokens": 900, "timeout": 45.5}


def test_an_empty_panel_field_keeps_the_preset_value(set_prefs):
    set_prefs(narrator_temperature="", narrator_max_tokens=None)
    assert ActionService._apply_sampling_prefs({"temperature": 0.3}) == {"temperature": 0.3}
    assert ActionService._apply_sampling_prefs(None) is None


def test_a_broken_number_is_ignored_not_fatal(set_prefs):
    set_prefs(narrator_temperature="hot", narrator_max_tokens=-5, narrator_timeout=0)
    assert ActionService._apply_sampling_prefs(None) is None


def test_the_option_layer_reads_every_knob(set_prefs):
    from app.api.routes_engine_actions import _suggestion_settings

    set_prefs(
        suggestions_count=8,          # clamped to 5
        suggestions_temperature=0.4,
        suggestions_max_tokens=250,
        suggestions_timeout=30,
        suggestions_extra_rules="优先给出危险动作",
        suggestions_allow_speech=False,
        suggestions_use_story=False,
    )
    chosen = _suggestion_settings()
    assert chosen["count"] == 5
    assert chosen["temperature"] == 0.4
    assert chosen["max_tokens"] == 250
    assert chosen["timeout"] == 30.0
    assert chosen["extra_rules"] == "优先给出危险动作"
    assert chosen["allow_speech"] is False
    assert chosen["allow_investigate"] is True
    assert chosen["use_story"] is False


def test_the_option_defaults_are_unchanged_when_nothing_is_set():
    from app.api.routes_engine_actions import _suggestion_settings

    chosen = _suggestion_settings()
    assert chosen["count"] == 3
    assert chosen["temperature"] == 0.8
    assert chosen["allow_speech"] is True


class _StubRouter:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.reply


def _guidance():
    return {
        "location": "对接舱",
        "exits": ["内舱"],
        "doors": ["气闸内门"],
        "keys": ["主管钥匙卡"],
        "here_items": [],
        "characters": ["艾拉"],
        "hostiles": [],
        "held": [],
        "quest": "",
        "in_combat": False,
        "language": "zh",
    }


def test_the_prompt_follows_the_count_and_the_switches():
    prompt = guidance_prompt(_guidance(), count=2, allow_speech=False, allow_investigate=False)
    assert "恰好 2 条" in prompt
    assert "不要生成「说话」类选项" in prompt
    assert "不要生成「检查／调查」类选项" in prompt
    # With talking off, nothing in the prompt may invite a spoken line.
    assert '"speech": "<一句台词>"' not in prompt
    assert "把台词原文放进 speech" not in prompt


def test_extra_rules_reach_the_prompt():
    prompt = guidance_prompt(_guidance(), extra_rules="每条都要有压迫感")
    assert "每条都要有压迫感" in prompt


def test_a_two_option_reply_is_kept_as_two():
    import asyncio

    reply = '[{"action": "走向内舱", "speech": "", "target": ""}, {"action": "拿起主管钥匙卡", "speech": "", "target": ""}]'
    router = _StubRouter(reply)
    options = asyncio.run(
        model_suggestions(router, _guidance(), count=2)
    )
    assert len(options) == 2
    assert router.calls[0]["max_tokens"] == 400
