"""The narrator reads two separate channels: authored canon and committed history."""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import ActionRequest, GameState, NarrativeResult
from app.services.action_service import ActionService
from app.services.narrative_service import (
    HISTORY_TURNS,
    LLMNarrator,
    render_history_note,
)
from tests.support import bound_world


class RecordingNarrator:
    def __init__(self):
        self.seen = []

    async def narrate(
        self,
        request,
        outcome,
        state,
        world,
        executions=(),
        quest_updates=(),
        knowledge=None,
        options=None,
    ):
        self.seen.append(list(knowledge or []))
        return NarrativeResult(status="ok", text="ok", model="recording")


class RecordingRouter:
    """Stands in for LLMRouter and keeps the prompt it was handed."""

    def __init__(self):
        self.messages = []

    async def complete(self, messages, **kwargs):
        self.messages.append(messages)
        return "The square is quiet."


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def _request(request_id, action, target=None):
    return ActionRequest(
        campaign_id="camp-1", request_id=request_id, action=action, target_id=target
    )


def _event(event_type, payload):
    return {"event_type": event_type, "payload": payload}


# ------------------------------------------------------------------- the recap
def test_history_note_reads_only_committed_turns():
    events = [
        _event("PLAYER_ACTION", {"action": "pick up the iron key"}),
        _event("NARRATOR_RESPONSE", {"text": "The key is cold in your hand."}),
        _event("INVENTORY", {"item_id": "iron_key"}),
        _event("STATE_COMMITTED", {"version": 2}),
    ]
    note = render_history_note(events)
    assert note.splitlines() == [
        "player: pick up the iron key",
        "narrator: The key is cold in your hand.",
    ]


def test_history_note_is_empty_without_dialogue():
    assert render_history_note([_event("INVENTORY", {"item_id": "iron_key"})]) == ""
    assert render_history_note([]) == ""


def test_history_note_keeps_the_recent_turns_only():
    events = []
    for index in range(HISTORY_TURNS * 3):
        events.append(_event("PLAYER_ACTION", {"action": f"step {index}"}))
    lines = render_history_note(events).splitlines()
    assert len(lines) == HISTORY_TURNS * 2
    assert lines[-1] == f"player: step {HISTORY_TURNS * 3 - 1}"


# ------------------------------------------------------- the two channels apart
async def test_canon_and_history_reach_the_narrator_apart(store):
    world, _state = bound_world(store)
    narrator = RecordingNarrator()
    service = ActionService(
        store,
        narrator=narrator,
        history_provider=lambda campaign_id, state: [
            {
                "id": "history:camp-1",
                "layer": "history",
                "title": "recent turns",
                "content": "player: look around",
            }
        ],
    )
    result = await service.execute(_request("req-1", "look"))
    assert result.status == "applied"
    delivered = [block for block in narrator.seen[-1]]
    assert delivered and all(block["kind"] == "history" for block in delivered)


async def test_no_history_provider_means_no_history_blocks(store):
    world, _state = bound_world(store)
    narrator = RecordingNarrator()
    service = ActionService(store, narrator=narrator)
    await service.execute(_request("req-1", "look"))
    assert narrator.seen[-1] == []


async def test_a_broken_history_provider_cannot_fail_the_turn(store):
    world, _state = bound_world(store)
    narrator = RecordingNarrator()

    def explode(campaign_id, state):
        raise RuntimeError("event log unreachable")

    service = ActionService(store, narrator=narrator, history_provider=explode)
    result = await service.execute(_request("req-1", "look"))
    assert result.status == "applied"
    assert result.state_version == 1
    assert store.get_state("camp-1").version == 1


async def test_a_rejected_action_also_gets_history(store):
    world, state = bound_world(store)
    narrator = RecordingNarrator()
    service = ActionService(
        store,
        narrator=narrator,
        history_provider=lambda campaign_id, state: [
            {"id": "h", "layer": "history", "title": "recent turns", "content": "x"}
        ],
    )
    result = await service.execute(_request("req-1", "open", "warehouse_door"))
    assert result.status == "rejected"
    assert [block["kind"] for block in narrator.seen[-1]] == ["history"]


# --------------------------------------------------------------- the prompt
def test_the_prompt_separates_canon_from_record():
    router = RecordingRouter()
    narrator = LLMNarrator(router, timeout=5)
    world, state = bound_world_light()
    payload = narrator._build_messages(
        _request("req-1", "look"),
        type("O", (), {"ok": True, "code": "looked", "message": "", "effects": []})(),
        state,
        world,
        (),
        knowledge=[
            {"kind": "canon", "layer": "global", "title": "Canon", "content": "The moon is broken."},
            {
                "kind": "history",
                "layer": "history",
                "title": "recent turns",
                "content": "player: look around",
            },
        ],
    )
    user = payload[1]["content"]
    assert "Authored world knowledge" in user
    assert "Already-committed history" in user
    assert user.index("Authored world knowledge") < user.index("Already-committed history")


def test_history_alone_does_not_claim_to_be_canon():
    narrator = LLMNarrator(RecordingRouter(), timeout=5)
    world, state = bound_world_light()
    payload = narrator._build_messages(
        _request("req-1", "look"),
        type("O", (), {"ok": True, "code": "looked", "message": "", "effects": []})(),
        state,
        world,
        (),
        knowledge=[{"kind": "history", "title": "recent turns", "content": "player: x"}],
    )
    user = payload[1]["content"]
    assert "Authored world knowledge" not in user
    assert "Already-committed history" in user


def bound_world_light():
    """A world + state pair built in memory, without touching a database."""
    from app.models.game import Effect
    from tests.support import village_world, fresh_state

    world = village_world()
    state = fresh_state(world, "camp-1")
    return world, state


def test_recap_reads_real_namedtuples_and_sorts_chronologically(tmp_path):
    from app.db.event_store import EventStore, EventType

    legacy = EventStore(str(tmp_path / "legacy.db"))
    try:
        first = legacy.append("camp-1", EventType.PLAYER_ACTION, {"text": "看看村庄"}, 0, "", [])
        second = legacy.append("camp-1", EventType.NARRATOR_RESPONSE, {"text": "街道安静。"}, 0, "", [])
        assert render_history_note([second, first]).splitlines() == [
            "player: 看看村庄", "narrator: 街道安静。",
        ]
    finally:
        legacy.close()


def test_recap_handles_engine_dicts_and_malformed_payloads():
    events = [None, 42, _event("PLAYER_ACTION", "[]"), _event("PLAYER_ACTION", "bad json")]
    events += [
        _event("action.applied", {"action": "look", "params": {"text": "look around"}}),
        _event("narrator.response", '{"text": "Quiet."}'),
        _event("NARRATOR_RESPONSE", {"text": {"invalid": "value"}}),
    ]
    assert render_history_note(events) == "player: look around\nnarrator: Quiet."
    assert render_history_note(events, limit=0) == ""


async def test_engine_recap_uses_committed_requests_not_legacy_mirrors(store, monkeypatch):
    from app.api import routes_engine

    bound_world(store)
    monkeypatch.setenv("GAME_STATE_DB_PATH", store.db_path)
    monkeypatch.setenv("EVENT_DB_PATH", store.db_path + ".legacy")
    narrator = RecordingNarrator()
    service = ActionService(store, narrator=narrator, history_provider=routes_engine._history_provider())
    await service.execute(_request("past", "look"))
    await service.execute(_request("refused", "open", "warehouse_door"))
    await service.execute(_request("current", "look"))
    history = narrator.seen[-1][0]["content"]
    assert history.splitlines() == ["player: look", "narrator: ok", "player: open warehouse_door", "narrator: ok"]
    assert store.get_state("camp-1").version == 2


def test_unbound_recap_falls_back_to_legacy_only(store, tmp_path, monkeypatch):
    from app.api import routes_engine
    from app.db.event_store import EventStore, EventType

    path = str(tmp_path / "legacy.db")
    monkeypatch.setenv("GAME_STATE_DB_PATH", store.db_path)
    monkeypatch.setenv("EVENT_DB_PATH", path)
    legacy = EventStore(path)
    try:
        legacy.append("old", EventType.PLAYER_ACTION, {"text": "old turn"}, 0, "", [])
        legacy.append("other", EventType.PLAYER_ACTION, {"text": "private"}, 0, "", [])
    finally:
        legacy.close()
    history = routes_engine._history_provider()("old", None)
    assert history[0]["content"] == "player: old turn"
