"""Narration diagnostics: what the model saw, which route answered, why not."

These are the fields the debug console reads, so they are asserted directly:
a capture that silently stays empty is worse than no capture at all.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.game import ActionRequest, Effect, GameState, NarrativeResult, RuleOutcome
from tests.support import fresh_state
from app.services.narrative_service import (
    BLOCK_EXCERPT_CHARS,
    LLMNarrator,
    TemplateNarrator,
    capture_diagnostics,
)
from tests.support import village_world


def _request(**kwargs):
    payload = {"request_id": "r1", "campaign_id": "camp-1", "action": "look"}
    payload.update(kwargs)
    return ActionRequest(**payload)


def _outcome(ok=True):
    return RuleOutcome(
        ok=ok,
        code="ok" if ok else "blocked",
        message="You look around." if ok else "You cannot do that.",
        effects=[Effect(kind="set_flag", target="seen", value="1")] if ok else [],
    )


class _Router:
    """A router that answers one fixed string and records the call."""

    class config:
        provider = "deepseek"
        model = "deepseek-chat"
        content_policy = "standard"

    def __init__(self, text="You look around.", error=None):
        self.text = text
        self.error = error
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.text


def test_capture_records_blocks_and_settings():
    blocks = [
        {
            "id": "canon:1",
            "layer": "global",
            "title": "The village",
            "content": "  A quiet place.  " * 40,
            "kind": "canon",
        },
        {"id": "longterm:camp-1", "layer": "history", "title": "长期摘要", "content": "You met Mira."},
    ]
    diag = capture_diagnostics(
        blocks,
        {"language": "中文", "temperature": 0.3},
        provider="openai",
        model="gpt-4o-mini",
        timeout=12.0,
    )
    assert [b.id for b in diag.blocks] == ["canon:1", "longterm:camp-1"]
    assert diag.blocks[0].excerpt.startswith("A quiet place.")
    assert len(diag.blocks[0].excerpt) <= BLOCK_EXCERPT_CHARS
    assert diag.language == "中文"
    assert diag.temperature == 0.3
    assert diag.provider == "openai"
    assert diag.timeout == 12.0


def test_capture_prefers_the_routed_preset_and_ignores_junk():
    diag = capture_diagnostics(
        None,
        {"provider": "local", "model": "qwen", "max_tokens": "big"},
        provider="openai",
        model="gpt-4o-mini",
        default_max_tokens=500,
    )
    assert (diag.provider, diag.model) == ("local", "qwen")
    assert diag.preset_routed is True
    assert diag.max_tokens == 500


def test_llm_narrator_reports_the_route_that_answered():
    world = village_world()
    state = fresh_state(world)
    router = _Router()
    narrator = LLMNarrator(router, language="English")
    result = asyncio.run(
        narrator.narrate(
            _request(),
            _outcome(),
            state,
            world,
            knowledge=[{"id": "canon:1", "layer": "global", "title": "T", "content": "C"}],
        )
    )
    assert result.status == "ok"
    assert result.diagnostics is not None
    assert result.diagnostics.provider == "deepseek"
    assert result.diagnostics.model == "deepseek-chat"
    assert result.diagnostics.timeout == narrator.timeout
    assert result.diagnostics.language == "English"
    assert [b.id for b in result.diagnostics.blocks] == ["canon:1"]


def test_llm_narrator_keeps_capture_on_failure():
    world = village_world()
    state = fresh_state(world)
    narrator = LLMNarrator(_Router(error=RuntimeError("upstream exploded")))
    result = asyncio.run(narrator.narrate(_request(), _outcome(), state, world))
    assert result.status == "failed"
    assert "upstream exploded" in result.error
    assert result.diagnostics is not None
    assert result.diagnostics.model == "deepseek-chat"


def test_llm_narrator_reports_a_refusal_reroute():
    world = village_world()
    state = fresh_state(world)

    class Refusing(_Router):
        async def complete(self, messages, **kwargs):
            self.calls.append({"kwargs": kwargs})
            if kwargs.get("content_policy") != "nsfw":
                raise RuntimeError("content policy violation")
            return "You look around."

    router = Refusing()
    result = asyncio.run(
        LLMNarrator(router).narrate(
            _request(), _outcome(), state, world, options={"content_policy": "standard"}
        )
    )
    assert result.status == "ok"
    assert result.diagnostics.content_policy == "nsfw"


def test_template_narrator_marks_the_capture_as_local():
    world = village_world()
    state = fresh_state(world)
    result = asyncio.run(TemplateNarrator().narrate(_request(), _outcome(), state, world))
    assert result.status == "ok"
    assert result.model == "template"
    assert result.diagnostics is not None
    assert result.diagnostics.skipped is True
    assert result.diagnostics.provider == "template"


def test_capture_counts_the_context_sources():
    blocks = [
        {"id": "canon:1", "layer": "global", "title": "A", "content": "a"},
        {"id": "canon:2", "layer": "global", "title": "B", "content": "b"},
        {"id": "history:camp-1", "layer": "history", "title": "recent turns", "content": "c", "kind": "history"},
        {"id": "", "title": "no kind", "content": "d"},
        "not a block",
    ]
    diag = capture_diagnostics(blocks)
    # Counted by kind when present, else by layer; unusable blocks are skipped
    # rather than counted under an empty key.
    assert diag.fact_counts == {"global": 2, "history": 1}


def test_capture_accepts_explicit_fact_counts():
    diag = capture_diagnostics([], None, fact_counts={"event": 3})
    assert diag.fact_counts == {"event": 3}


def test_narrator_timeout_records_the_answering_policy():
    world = village_world()
    state = fresh_state(world)

    class Slow(_Router):
        async def complete(self, messages, **kwargs):
            raise asyncio.TimeoutError()

    narrator = LLMNarrator(Slow(), timeout=0.01)
    result = asyncio.run(
        narrator.narrate(
            _request(), _outcome(), state, world, options={"content_policy": "nsfw"}
        )
    )
    assert result.status == "failed"
    assert result.diagnostics is not None
    # A failed call still has to say which policy was in force, otherwise a
    # reviewer cannot tell a standard turn from a rerouted nsfw one.
    assert result.diagnostics.content_policy == "nsfw"


def test_narrator_empty_text_records_the_answering_policy():
    world = village_world()
    state = fresh_state(world)
    narrator = LLMNarrator(_Router(text="   "))
    result = asyncio.run(
        narrator.narrate(
            _request(), _outcome(), state, world, options={"content_policy": "nsfw"}
        )
    )
    assert result.status == "failed"
    assert "empty" in result.error
    assert result.diagnostics.content_policy == "nsfw"
