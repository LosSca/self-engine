"""Narrator contract: settled facts in, prose out, never state changes."""
import asyncio
import json

import pytest

from app.models.game import ActionRequest, GameState, NarrativeResult, RuleOutcome, WorldDefinition
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


async def test_language_is_per_call_not_shared_narrator_state():
    world, state, request, outcome = _setup()
    router = StubRouter()
    narrator = LLMNarrator(router, language="en")
    await narrator.narrate(request, outcome, state, world, options={"language": "zh"})
    await narrator.narrate(request, outcome, state, world)
    assert "Reply in zh " in router.calls[0]["messages"][0]["content"]
    assert "Reply in en " in router.calls[1]["messages"][0]["content"]
    assert narrator.language == "en"


@pytest.mark.parametrize("status", ["fired", "failed", "skipped"])
async def test_event_description_is_advisory_and_success_only(status):
    from app.models.game import EventExecution, GameEventDef

    world, state, request, outcome = _setup()
    description = "守门人的脚步声在雾里回响。"
    world.events = [GameEventDef(id="voice", name="Footsteps", description=description)]
    execution = EventExecution(event_id="voice", name="Footsteps", status=status)
    before = state.model_dump_json()
    template = await TemplateNarrator().narrate(request, outcome, state, world, [execution])
    router = StubRouter()
    await LLMNarrator(router).narrate(request, outcome, state, world, [execution])
    prompt = router.calls[0]["messages"][1]["content"]
    assert (description in template.text) == (status == "fired")
    assert (description in prompt) == (status == "fired")
    if status == "fired":
        facts = json.loads(prompt.split("Settled game facts (JSON):\n", 1)[1].split("\n\nAuthored", 1)[0])
        assert description not in json.dumps(facts, ensure_ascii=False)
        assert "background only" in prompt
        assert "never overrides the settled facts" in prompt
    assert state.model_dump_json() == before


def test_preset_router_builds_the_authored_provider_and_model():
    from app.services.narrative_service import preset_router
    from app.engines.llm_router import LLMProvider

    router = preset_router({"provider": "openai", "model": "gpt-test"})
    assert router is not None
    assert router.config.primary_provider == LLMProvider.OPENAI
    assert router.config.primary_model == "gpt-test"
    assert preset_router({"provider": "", "model": ""}) is None
    assert preset_router({"provider": "nope", "model": "x"}) is None


@pytest.mark.asyncio
async def test_narrator_prefers_the_scene_preset_router(monkeypatch):
    from app.services import narrative_service

    class Stub:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, **kwargs):
            self.calls.append(kwargs)
            return "preset prose"

    preset = Stub()
    default = Stub()
    monkeypatch.setattr(narrative_service, "preset_router", lambda options: preset)
    narrator = narrative_service.LLMNarrator(default, language="zh")
    result = await narrator.narrate(
        ActionRequest(campaign_id="c", request_id="r", action="look"),
        RuleOutcome(ok=True, message="ok"),
        GameState(campaign_id="c", world_id="w"),
        WorldDefinition(world_id="w", name="w", start_location_id="x"),
        options={"provider": "openai", "model": "gpt-test"},
    )
    assert result.status == "ok"
    assert result.text == "preset prose"
    assert len(preset.calls) == 1 and not default.calls


@pytest.mark.asyncio
async def test_narrator_system_prompt_override_keeps_rules():
    router = StubRouter()
    narrator = LLMNarrator(router, language="zh")
    state = build_initial_state(village_world(), "c")
    result = await narrator.narrate(
        ActionRequest(campaign_id="c", request_id="r", action="look"),
        RuleOutcome(ok=True, message="ok"),
        state,
        village_world(),
        options={"system_prompt": "Use terse, cold prose."},
    )
    assert result.status == "ok"
    system = router.calls[-1]["messages"][0]["content"]
    assert system.startswith("Use terse, cold prose.")
    assert "Never invent item gains" in system


@pytest.mark.asyncio
async def test_summary_service_returns_bounded_text_and_fails_silently():
    from app.services.summary_service import summarize

    class Stub:
        def __init__(self, text):
            self.text = text

        async def complete(self, messages, **kwargs):
            return self.text

    long_text = "summary line " * 200
    result = await summarize(Stub(long_text), "[T1] recap")
    assert result and len(result) <= 1200
    assert await summarize(Stub(""), "[T1] recap") == ""
    assert await summarize(None, "[T1] recap") == ""

    class Broken:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("down")

    assert await summarize(Broken(), "[T1] recap") == ""
