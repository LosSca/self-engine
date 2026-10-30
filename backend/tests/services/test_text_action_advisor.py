"""An unmapped utterance stays a safe rejection unless the advisor is enabled."""
import asyncio

import pytest

from app.db.game_state_store import GameStateStore
from app.services.intent_advisor import Suggestion
from app.services.text_action_service import TextActionService
from tests.support import bound_world


class _FakeAdvisor:
    def __init__(self, action=None, fragment=""):
        self.action = action
        self.fragment = fragment
        self.calls = []

    async def suggest(self, text, world, extra_actions=()):
        self.calls.append(text)
        if self.action is None:
            return None
        return Suggestion(action=self.action, fragment=self.fragment, confidence=0.5)


@pytest.fixture()
def store(tmp_path):
    path = tmp_path / "state.db"
    store = GameStateStore(str(path))
    yield store
    store.close()


@pytest.fixture()
def service(store):
    world, _state = bound_world(store, "camp-1")
    return TextActionService(store)


UNPARSED = "the thing with the key, you know, do that"


def test_without_the_advisor_the_utterance_is_rejected(service, monkeypatch):
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    out = asyncio.run(service.execute("camp-1", UNPARSED))
    assert out["status"] == "rejected"
    assert out["result"]["code"] == "unparsed_action"
    assert service.game_store.get_state("camp-1").version == 0


def test_the_advisor_is_not_even_asked_when_disabled(service, monkeypatch):
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    advisor = _FakeAdvisor(action="look", fragment="around")
    service.advisor = advisor
    asyncio.run(service.execute("camp-1", UNPARSED))
    assert advisor.calls == []


def test_an_enabled_advisor_can_rescue_an_unmapped_utterance(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = _FakeAdvisor(action="look", fragment="around")
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r1"))
    assert out["status"] == "applied", out
    assert out["parsed"]["note"].endswith("intent advisor") or "intent advisor" in out["parsed"]["note"]
    assert service.game_store.get_state("camp-1").version == 1


def test_the_suggestion_still_has_to_name_a_real_target(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = _FakeAdvisor(action="take", fragment="a golden goose")
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r2"))
    assert out["status"] != "applied"
    assert service.game_store.get_state("camp-1").version == 0


def test_the_rules_still_decide_the_outcome(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    # The door is locked and the key is on the ground: the advisor may suggest
    # "open", but the rule engine refuses, exactly as it would without it.
    service.advisor = _FakeAdvisor(action="open", fragment="warehouse door")
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r3"))
    assert out["status"] == "rejected"
    assert out["result"]["code"] == "door_locked"
    assert service.game_store.get_state("camp-1").version == 0


def test_an_advisor_that_is_enabled_but_missing_keeps_the_rejection(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = None
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r4"))
    assert out["status"] == "rejected"
    assert out["result"]["code"] == "unparsed_action"


def test_a_recognised_utterance_never_reaches_the_advisor(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    advisor = _FakeAdvisor(action="look", fragment="around")
    service.advisor = advisor
    out = asyncio.run(service.execute("camp-1", "look around", request_id="r5"))
    assert out["status"] == "applied"
    assert advisor.calls == []


def test_unsupported_sentence_with_a_known_noun_reaches_optional_advisor(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    advisor = _FakeAdvisor(action="open", fragment="warehouse door")
    service.advisor = advisor
    text = "sneak past elder_mara"
    out = asyncio.run(service.execute("camp-1", text, request_id="stealth"))
    assert advisor.calls == [text]
    assert out["status"] == "rejected"
    assert out["result"]["code"] == "door_locked"
    assert service.game_store.get_state("camp-1").version == 0


def test_advisor_cannot_substitute_a_different_action_for_an_explicit_verb(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    advisor = _FakeAdvisor(action="take", fragment="iron_key")
    service.advisor = advisor
    out = asyncio.run(service.execute("camp-1", "open iron_key", request_id="wrong-verb"))
    assert advisor.calls == []
    assert out["parsed"]["action"] == "open"
    assert out["status"] == "rejected"
    assert service.game_store.get_state("camp-1").version == 0


@pytest.mark.parametrize("text", ["[SAY] take iron_key", "[THINK] take iron_key", "[META] take iron_key", "[CONTINUE]"])
def test_advisor_must_not_turn_non_action_ui_modes_into_physical_actions(service, monkeypatch, text):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = _FakeAdvisor(action="take", fragment="iron_key")
    out = asyncio.run(service.execute("camp-1", text, request_id="envelope"))
    assert out["status"] == "rejected"
    assert service.game_store.get_state("camp-1").version == 0


def test_advisor_can_propose_a_supported_talk_for_speech(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = _FakeAdvisor(action="talk", fragment="elder_mara")
    out = asyncio.run(service.execute("camp-1", "[SAY] Hello elder_mara", request_id="speech"))
    assert out["status"] == "applied"
    assert out["parsed"]["action"] == "talk"
    assert out["parsed"]["target_id"] == "elder_mara"


def test_advisor_missing_talk_target_stays_a_rejection(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = _FakeAdvisor(action="talk", fragment="nonexistent")
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="bad-target"))
    assert out["status"] == "rejected"
    assert out["result"]["code"] == "missing_target"
    assert service.game_store.get_state("camp-1").version == 0
