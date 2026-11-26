"""The advisor's reading is written down, so the same sentence is free next time."""
import asyncio

import pytest

from app.db.intent_memory_store import IntentMemoryStore, normalise_phrase
from app.db.game_state_store import GameStateStore
from app.services.intent_advisor import Suggestion
from app.services.text_action_service import TextActionService
from tests.support import bound_world


class _CountingAdvisor:
    def __init__(self, action="look", fragment="around"):
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
    game_store = GameStateStore(str(tmp_path / "state.db"))
    yield game_store
    game_store.close()


@pytest.fixture()
def service(store):
    bound_world(store, "camp-1")
    return TextActionService(store)


UNPARSED = "the thing with the key, you know, do that"


def test_the_key_ignores_the_envelope_case_and_punctuation():
    """Two ways of asking the same thing must share one row."""
    assert normalise_phrase("[DO] 推开门。") == normalise_phrase("推开门")
    assert normalise_phrase("[SAY] Hello there!") == normalise_phrase("hello   there")


def test_a_learning_is_scoped_to_its_world(tmp_path):
    path = str(tmp_path / "intent.db")
    store = IntentMemoryStore(path)
    try:
        store.remember("world-a", "推开门", "open", "门")
        assert store.recall("world-a", "推开门")["action"] == "open"
        # The same words in another world were never taught.
        assert store.recall("world-b", "推开门") is None
    finally:
        store.close()


def test_a_repeat_reading_bumps_the_hit_count(tmp_path):
    store = IntentMemoryStore(str(tmp_path / "intent.db"))
    try:
        store.remember("w", "推开门", "open", "门")
        assert store.recall("w", "推开门")["hits"] == 2
        assert store.recall("w", "推开门")["hits"] == 3
    finally:
        store.close()


def test_the_advisor_reading_is_written_to_the_table(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    advisor = _CountingAdvisor()
    service.advisor = advisor
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r1"))
    assert out["status"] == "applied", out
    assert advisor.calls == [UNPARSED]
    world_id = service._world_for_state(
        service.game_store.get_state("camp-1"), "camp-1"
    ).world_id
    rows = IntentMemoryStore().list(world_id)
    assert [row["action"] for row in rows] == ["look"], rows
    assert rows[0]["phrase"] == normalise_phrase(UNPARSED)


def test_the_same_sentence_twice_calls_the_advisor_once(service, monkeypatch):
    """G4: a learned reading is reused, so the second turn costs no model call."""
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    advisor = _CountingAdvisor()
    service.advisor = advisor
    first = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r1"))
    assert first["status"] == "applied", first
    # A fresh service, as a later request would build: the memory is in the
    # table, not in the object.
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    second_service = TextActionService(service.game_store)
    second_service.advisor = advisor
    second = asyncio.run(second_service.execute("camp-1", UNPARSED, request_id="r2"))
    assert second["status"] == "applied", second
    assert advisor.calls == [UNPARSED], advisor.calls
    assert "intent memory" in second["parsed"]["note"]


def test_a_learned_reading_still_works_with_the_advisor_switched_off(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    service.advisor = _CountingAdvisor()
    asyncio.run(service.execute("camp-1", UNPARSED, request_id="r1"))
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    advisor = _CountingAdvisor()
    service.advisor = advisor
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r2"))
    assert out["status"] == "applied", out
    assert advisor.calls == []


def test_a_stale_reading_is_not_echoed(service, monkeypatch):
    """A row that no longer resolves against the world must fall through."""
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    world = service._world_for_state(service.game_store.get_state("camp-1"), "camp-1")
    store = IntentMemoryStore()
    try:
        store.remember(world.world_id, UNPARSED, "take", "a golden goose")
    finally:
        store.close()
    advisor = _CountingAdvisor()
    service.advisor = advisor
    out = asyncio.run(service.execute("camp-1", UNPARSED, request_id="r1"))
    # The stored target is not in this world, so the row was skipped and the
    # advisor was asked again; its (still invalid) suggestion did not survive
    # either, so the utterance stays the attempt it always was.
    assert advisor.calls == [UNPARSED]
    assert out["parsed"]["action"] != "take", out["parsed"]
    assert "intent memory" not in (out["parsed"].get("note") or "")


def test_speech_only_reuses_a_talk_reading(service, monkeypatch):
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    world = service._world_for_state(service.game_store.get_state("camp-1"), "camp-1")
    store = IntentMemoryStore()
    try:
        store.remember(world.world_id, "[SAY] hello there", "look", "around")
    finally:
        store.close()
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    out = asyncio.run(service.execute("camp-1", "[SAY] hello there", request_id="r1"))
    # A look reading must not answer a spoken line.
    assert out["parsed"]["action"] != "look"
