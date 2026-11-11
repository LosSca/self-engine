"""Narration must describe committed state, and never leave the player stuck."""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import ActionRequest, Effect, EventExecution, NarrativeResult, RuleOutcome
from app.services.action_service import ActionService
from app.services.narrative_service import (
    LLMNarrator,
    _reward_announcements,
    render_fallback,
)
from tests.support import bound_world, village_world


class FailingNarrator:
    """Reports a failure instead of raising: the model is simply unavailable."""

    def __init__(self):
        self.calls = 0

    async def narrate(self, *args, **kwargs):
        self.calls += 1
        return NarrativeResult(status="failed", error="model unavailable")


class ExplodingNarrator:
    def __init__(self):
        self.calls = 0

    async def narrate(self, *args, **kwargs):
        self.calls += 1
        raise TimeoutError("model call timed out")


class StubRouter:
    """Stands in for LLMRouter: returns whatever prose the test wants."""

    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def complete(self, messages, max_tokens=0, temperature=0, **kwargs):
        self.calls += 1
        return self.text


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def _request(request_id, action, target=None):
    return ActionRequest(
        campaign_id="camp-1", request_id=request_id, action=action, target_id=target
    )


# ------------------------------------------------------- fallback narration
async def test_failed_narrator_still_describes_the_committed_turn(store):
    bound_world(store)
    narrator = FailingNarrator()
    service = ActionService(store, narrator=narrator)

    result = await service.execute(_request("req-take", "take", "iron_key"))

    assert result.status == "applied"
    assert result.state.version == 1
    assert result.state.inventory == {"iron_key": 1}
    # The player is not handed an error: the engine describes its own result,
    # and the model being down stays visible on the same record.
    assert result.narrative.status == "failed"
    assert result.narrative.model == "fallback"
    assert "Iron Key" in result.narrative.text
    # The failure is still reported for debugging.
    assert "model unavailable" in result.narrative.error
    assert narrator.calls == 1


async def test_exploding_narrator_also_falls_back(store):
    bound_world(store)
    service = ActionService(store, narrator=ExplodingNarrator())

    result = await service.execute(_request("req-open-rejected", "open", "warehouse_door"))

    assert result.status == "rejected"
    assert result.code == "door_locked"
    assert result.narrative.status == "ok"
    assert result.narrative.model == "fallback"
    assert "locked" in result.narrative.text.lower()
    assert "TimeoutError" in result.narrative.error


def test_render_fallback_names_effects_and_events():
    world = village_world()
    from app.services.world_service import build_initial_state

    state = build_initial_state(world, "camp-1")
    outcome = RuleOutcome(
        ok=True,
        code="taken",
        message="",
        effects=[Effect(kind="give_item", target="iron_key")],
    )
    executions = [
        EventExecution(event_id="ev1", name="The wind rises", status="fired"),
        EventExecution(event_id="ev2", name="Broken", status="failed", reason="bad target"),
    ]
    text = render_fallback(world, state, outcome, executions)
    assert "Iron Key" in text
    assert "The wind rises" in text
    assert "bad target" in text


# ----------------------------------------------- reward-claim verification
def test_unbacked_reward_is_detected_in_english_and_chinese():
    assert _reward_announcements("A cheer. You gain 50 gold and smile.") == ["You gain"]
    assert _reward_announcements("你获得了 50 金币。") == ["你获得了"]
    assert _reward_announcements("You open the door and step outside.") == []


async def test_llm_prose_that_invents_a_reward_is_rejected():
    world = village_world()
    from app.services.world_service import build_initial_state

    state = build_initial_state(world, "camp-1")
    router = StubRouter("You take the key and gain 50 gold.")
    narrator = LLMNarrator(router)
    outcome = RuleOutcome(ok=True, code="taken", effects=[Effect(kind="give_item", target="iron_key")])

    result = await narrator.narrate(
        _request("req-1", "take", "iron_key"), outcome, state, world
    )

    assert result.status == "failed"
    assert "did not award" in result.error


async def test_llm_prose_naming_the_awarded_item_is_accepted():
    world = village_world()
    from app.services.world_service import build_initial_state

    state = build_initial_state(world, "camp-1")
    router = StubRouter("You pick up the Iron Key; it is cold in your hand.")
    narrator = LLMNarrator(router)
    outcome = RuleOutcome(ok=True, code="taken", effects=[Effect(kind="give_item", target="iron_key")])

    result = await narrator.narrate(
        _request("req-1", "take", "iron_key"), outcome, state, world
    )

    assert result.status == "ok"
    assert getattr(result, "model", "") == "llm"


async def test_event_effects_count_as_awarded():
    """An item handed out by a fired event is not an invented reward."""
    world = village_world()
    from app.services.world_service import build_initial_state

    state = build_initial_state(world, "camp-1")
    router = StubRouter("You receive the Blessing Token from your quest.")
    narrator = LLMNarrator(router)
    outcome = RuleOutcome(ok=True, code="door_unlocked", effects=[])
    execution = EventExecution(
        event_id="reward",
        name="Reward",
        status="fired",
        effects=[Effect(kind="give_item", target="blessing_token")],
    )

    result = await narrator.narrate(
        _request("req-2", "open", "warehouse_door"),
        outcome,
        state,
        world,
        [execution],
        executions_effects=[Effect(kind="give_item", target="blessing_token")],
    )

    assert result.status == "ok", result.error
