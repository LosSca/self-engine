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


def test_without_the_advisor_the_utterance_is_attempted_not_rejected(service, monkeypatch):
    """Free text is the point of the form: an unmapped sentence is an attempt.

    A vague wish ("the thing with the key, you know, do that") maps to no verb,
    so it is recorded as what it is - an attempt - and the rule engine changes
    nothing else. Refusing it was the old behaviour and taught nobody anything.
    """
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    out = asyncio.run(service.execute("camp-1", UNPARSED))
    assert out["status"] == "applied", out
    assert out["parsed"]["action"] == "improvise"
    assert out["result"]["code"] == "improvised"
    assert service.game_store.get_state("camp-1").version == 1


@pytest.mark.parametrize(
    "text",
    [
        "按门把手",
        "朝门外喊了一嗓子",
        "蹲下看看裂缝",
        "the thing with the key, you know, do that",
    ],
)
def test_any_free_form_attempt_has_a_home(service, text):
    """Whatever the player types, it becomes an action, not a parse error."""
    out = asyncio.run(service.execute("camp-1", f"[DO] {text}"))
    assert out["status"] == "applied", out
    assert out["parsed"]["action"] == "improvise"
    assert out["result"]["code"] == "improvised"


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


def test_an_advisor_that_is_enabled_but_missing_still_leaves_an_attempt(service, monkeypatch):
    """No model is not a dead end any more: the attempt still happens."""
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = None
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r4"))
    assert out["status"] == "applied"
    assert out["parsed"]["action"] == "improvise"
    assert out["result"]["code"] == "improvised"


def test_a_recognised_utterance_never_reaches_the_advisor(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    advisor = _FakeAdvisor(action="look", fragment="around")
    service.advisor = advisor
    out = asyncio.run(service.execute("camp-1", "look around", request_id="r5"))
    assert out["status"] == "applied"
    assert advisor.calls == []


def test_unsupported_sentence_with_a_known_noun_reaches_optional_advisor(service, monkeypatch):
    """The advisor runs first: a sentence it can map beats a bare attempt."""
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    advisor = _FakeAdvisor(action="open", fragment="warehouse door")
    service.advisor = advisor
    text = "sneak past elder_mara"
    out = asyncio.run(service.execute("camp-1", text, request_id="stealth"))
    assert advisor.calls == [text]
    assert out["result"]["code"] == "door_locked"
    assert out["parsed"]["action"] == "open"
    assert service.game_store.get_state("camp-1").version == 0


def test_without_the_advisor_the_same_sentence_is_only_an_attempt(service, monkeypatch):
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    out = asyncio.run(service.execute("camp-1", "sneak past elder_mara", request_id="stealth-2"))
    assert out["parsed"]["action"] == "improvise"
    assert out["result"]["code"] == "improvised"


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


def test_speech_is_addressed_to_whoever_is_here_without_a_model(service, monkeypatch):
    """Typed speech does not need an advisor to find its audience."""
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    out = asyncio.run(service.execute("camp-1", "[SAY] Hello there", request_id="speech"))
    assert out["parsed"]["action"] == "talk"
    # Nobody is standing in the demo world's square: the rule says so, and the
    # player still got an answer from the parser instead of a parse error.
    assert out["parsed"]["confidence"] in ("spoken", "missing_target")


def test_advisor_missing_talk_target_stays_a_rejection(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = _FakeAdvisor(action="talk", fragment="nonexistent")
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="bad-target"))
    assert out["status"] == "rejected"
    assert out["result"]["code"] == "missing_target"
    assert service.game_store.get_state("camp-1").version == 0
