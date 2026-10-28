"""Narrator contract: settled facts in, prose out, never state changes."""
import asyncio
import json

import pytest

from app.models.game import ActionRequest, NarrativeResult, RuleOutcome
from app.services.narrative_service import LLMNarrator, TemplateNarrator
from app.services.world_service import build_initial_state
from tests.support import village_world


class StubRouter:
    """Stands in for LLMRouter.complete (which must never receive model=)."""

    def __init__(self, reply="The door swings open.", delay=0.0, error=None):
        self.reply = reply
        self.delay = delay
        self.error = error
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


def _setup():
    world = village_world()
    state = build_initial_state(world, "camp-1")
    state.inventory = {"iron_key": 1}
    state.doors["warehouse_door"] = True
    state.quests["open_the_warehouse"].status = "completed"
    state.quests["open_the_warehouse"].objectives = {"find_key": 1, "open_door": 1}
    request = ActionRequest(
        campaign_id="camp-1", request_id="req-1", action="open", target_id="warehouse_door"
    )
    outcome = RuleOutcome(ok=True, code="door_unlocked", message="You unlock the door.")
    return world, state, request, outcome


async def test_llm_narrator_only_sends_committed_facts():
    world, state, request, outcome = _setup()
    router = StubRouter()
    narrator = LLMNarrator(router)

    result = await narrator.narrate(request, outcome, state, world)

    assert result.status == "ok"
    assert result.model == "llm"
    assert router.calls, "the narrator must call the router"
    kwargs = router.calls[0]["kwargs"]
    assert "model" not in kwargs, "LLMRouter picks the model itself"
    assert kwargs["max_tokens"] == narrator.max_tokens
    system, user = router.calls[0]["messages"]
    assert "already" in system["content"].lower(), "the engine verdict must be framed as past"
    assert "never invent" in system["content"].lower()
    facts = user["content"]
    assert "Settled game facts" in facts
    assert "Iron Key" in facts, "held items are shown by name"
    assert "warehouse door" in facts, "open doors are shown by name"
    assert "[completed]" in facts
    assert "door_unlocked" in facts


async def test_llm_narrator_strips_control_tags_from_prose():
    world, state, request, outcome = _setup()
    router = StubRouter(reply="You push the door. [ITEM_ADD:blessing_token|Blessing|Token] It gives.")
    result = await LLMNarrator(router).narrate(request, outcome, state, world)
    assert result.status == "ok"
    assert "ITEM_ADD" not in result.text
    assert "You push the door" in result.text


async def test_llm_narrator_times_out_without_touching_state():
    world, state, request, outcome = _setup()
    before = state.model_dump_json()
    router = StubRouter(delay=0.5)
    narrator = LLMNarrator(router, timeout=0.05)

    result = await narrator.narrate(request, outcome, state, world)

    assert result.status == "failed"
    assert "timed out" in result.error
    assert state.model_dump_json() == before


async def test_llm_narrator_reports_router_errors():
    world, state, request, outcome = _setup()
    router = StubRouter(error=RuntimeError("upstream 503"))
    result = await LLMNarrator(router).narrate(request, outcome, state, world)
    assert result.status == "failed"
    assert "upstream 503" in result.error


async def test_llm_narrator_rejects_empty_output():
    world, state, request, outcome = _setup()
    result = await LLMNarrator(StubRouter(reply="   ")).narrate(request, outcome, state, world)
    assert result.status == "failed"
    assert "empty" in result.error


async def test_template_narrator_is_deterministic_and_reads_facts():
    world, state, request, outcome = _setup()
    narrator = TemplateNarrator()
    first = await narrator.narrate(request, outcome, state, world)
    second = await narrator.narrate(request, outcome, state, world)
    assert first.status == "ok"
    assert first.model == "template"
    assert first.text == second.text
    assert "unlock the door" in first.text


async def test_template_narrator_reports_refusals_verbatim():
    world, state, request, _ = _setup()
    refusal = RuleOutcome(ok=False, code="door_locked", message="The door is locked.")
    result = await TemplateNarrator().narrate(request, refusal, state, world)
    assert result.status == "ok"
    assert result.text == "The door is locked."
