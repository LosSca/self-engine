"""Narrator-level content policy: the reroute must not double the wait."""
from __future__ import annotations

import asyncio

import pytest

from app.models.game import ActionRequest, Effect, GameState, RuleOutcome, WorldDefinition
from app.services import narrative_service
from app.services.narrative_service import LLMNarrator

WORLD = WorldDefinition(world_id="w", name="W", start_location_id="village")
STATE = GameState(campaign_id="c", world_id="w", location_id="village")
REQUEST = ActionRequest(
    request_id="r1", campaign_id="c", action="look", actor_id="player"
)
OUTCOME = RuleOutcome(ok=True, code="ok", message="looked", effects=[])


class _Router:
    """Router stub: each call pops one behaviour (an exception or a string)."""

    def __init__(self, behaviours, content_policy="standard"):
        self.behaviours = list(behaviours)
        self.policies = []
        self.config = type("Config", (), {"content_policy": content_policy})()

    async def complete(self, messages, **kwargs):
        self.policies.append(kwargs.get("content_policy"))
        behaviour = self.behaviours.pop(0)
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour


@pytest.fixture()
def no_wait(monkeypatch):
    monkeypatch.setattr(narrative_service, "REFUSAL_RETRY_DELAY", 0.0)


def _narrate(router):
    narrator = LLMNarrator(router)
    narrator._build_messages = lambda *a, **k: [{"role": "user", "content": "x"}]
    return asyncio.run(narrator.narrate(REQUEST, OUTCOME, STATE, WORLD))


def test_a_refusal_error_is_retried_on_the_local_policy(no_wait):
    router = _Router([RuntimeError("content_policy violation"), "The minute passes."])
    result = _narrate(router)
    assert result.status == "ok"
    assert router.policies == ["standard", "nsfw"]


def test_an_unrelated_error_is_not_retried(no_wait):
    router = _Router([ValueError("boom"), "should not be used"])
    result = _narrate(router)
    assert result.status == "failed"
    assert "boom" in result.error
    assert router.policies == ["standard"]


def test_the_configured_policy_travels_to_the_router(no_wait):
    router = _Router(["Prose."], content_policy="nsfw")
    assert _narrate(router).status == "ok"
    assert router.policies == ["nsfw"]


def test_a_double_timeout_does_not_retry(no_wait):
    # Waiting twice for a stalled model would double a delay the player already
    # paid; the turn fails fast and the committed state is untouched.
    router = _Router([asyncio.TimeoutError(), "never reached"])

    async def slow_complete(messages, **kwargs):
        router.policies.append(kwargs.get("content_policy"))
        raise asyncio.TimeoutError()

    router.complete = slow_complete
    narrator = LLMNarrator(router, timeout=0.01)
    narrator._build_messages = lambda *a, **k: [{"role": "user", "content": "x"}]
    result = asyncio.run(narrator.narrate(REQUEST, OUTCOME, STATE, WORLD))
    assert result.status == "failed"
    assert "timed out" in result.error
    assert router.policies == ["standard"]
