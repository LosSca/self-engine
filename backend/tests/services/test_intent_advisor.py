"""The advisor may suggest; the parser and the rule engine still decide."""
import asyncio

import pytest

from app.services import intent_advisor
from app.services.intent_advisor import IntentAdvisor, parse_reply


# ------------------------------------------------------------------ switch
def test_the_advisor_is_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    assert intent_advisor.enabled() is False
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("ENGINE_INTENT_ADVISOR", value)
        assert intent_advisor.enabled() is False, value
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("ENGINE_INTENT_ADVISOR", value)
        assert intent_advisor.enabled() is True, value


def test_the_timeout_is_bounded(monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_TIMEOUT", "0.01")
    assert intent_advisor.timeout_seconds() == 0.5
    monkeypatch.setenv("ENGINE_INTENT_TIMEOUT", "999")
    assert intent_advisor.timeout_seconds() == 30.0
    monkeypatch.setenv("ENGINE_INTENT_TIMEOUT", "not a number")
    assert intent_advisor.timeout_seconds() == intent_advisor.DEFAULT_TIMEOUT


# ------------------------------------------------------------------ parsing
def test_a_clean_reply_becomes_a_suggestion():
    suggestion = parse_reply(
        '{"action": "take", "target": "iron key", "confidence": 0.8}',
        ["take", "open"],
    )
    assert suggestion is not None
    assert suggestion.action == "take"
    assert suggestion.fragment == "iron key"
    assert suggestion.confidence == pytest.approx(0.8)


def test_a_fenced_reply_is_unwrapped():
    reply = 'Sure!\n```json\n{"action": "open", "target": "door", "confidence": 1}\n```'
    suggestion = parse_reply(reply, ["take", "open"])
    assert suggestion is not None and suggestion.action == "open"


def test_an_action_outside_the_whitelist_is_refused():
    assert parse_reply('{"action": "delete_save", "target": "x"}', ["take"]) is None


def test_garbage_is_refused():
    assert parse_reply("I think you should open the door", ["open"]) is None
    assert parse_reply("", ["open"]) is None
    assert parse_reply('["open"]', ["open"]) is None


def test_a_missing_confidence_is_zero_not_trusted():
    suggestion = parse_reply('{"action": "look"}', ["look"])
    assert suggestion is not None and suggestion.confidence == 0.0


# ------------------------------------------------------------------ advisor
class _Router:
    def __init__(self, reply=None, raises=None, delay=0.0):
        self.reply = reply
        self.raises = raises
        self.delay = delay
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.reply


def test_the_advisor_returns_the_parsed_suggestion():
    router = _Router('{"action": "open", "target": "warehouse door", "confidence": 0.7}')
    advisor = IntentAdvisor(router, timeout=1.0)
    suggestion = asyncio.run(advisor.suggest("please get that door open", world=None))
    assert suggestion is not None
    assert suggestion.action == "open"
    assert suggestion.fragment == "warehouse door"
    assert router.calls, "the router should have been asked"
    assert router.calls[0][1]["temperature"] == 0.0


def test_a_slow_model_is_skipped_instead_of_stalling_the_turn():
    router = _Router('{"action": "open"}', delay=0.5)
    advisor = IntentAdvisor(router, timeout=0.05)
    assert asyncio.run(advisor.suggest("open it", world=None)) is None


def test_a_broken_model_degrades_to_nothing():
    router = _Router(raises=RuntimeError("model offline"))
    advisor = IntentAdvisor(router, timeout=1.0)
    assert asyncio.run(advisor.suggest("open it", world=None)) is None
    assert asyncio.run(IntentAdvisor(None).suggest("open it", world=None)) is None
