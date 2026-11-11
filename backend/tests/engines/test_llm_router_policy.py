"""Content policy: NSFW routing and the passive refusal reroute.

The router owns the policy so every caller (narration, generation, NPC speech)
obbeys one switch. Two halves are pinned here:

- active routing: content_policy="nsfw" sends the turn to the NSFW/local model;
- passive routing: an upstream that answers (or errors) with a refusal is
  retried once on the local model, and a dead local server degrades to the
  primary provider instead of failing the turn.
"""
from __future__ import annotations

import asyncio

import pytest

from app.engines import llm_router
from app.engines.llm_router import (
    POLICY_CHOICES,
    REFUSAL_SIGNAL,
    LLMConfig,
    LLMProvider,
    LLMRouter,
    _looks_like_refusal,
)
from app.services import narrator_factory


class _Message:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Message(content)


class _Response:
    """Only the attributes _log_call actually reads."""

    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class _Delta:
    def __init__(self, content):
        self.content = content


class _StreamChoice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content):
        self.choices = [_StreamChoice(content)]


def _stream(text):
    async def _gen():
        for char in text:
            yield _Chunk(char)
        return
    return _gen()


class _Recorder:
    """Fake litellm that records which provider/model each call went to."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.models = []

    async def __call__(self, **kwargs):
        self.models.append(kwargs["model"])
        result = self.behaviour(kwargs["model"])
        if isinstance(result, Exception):
            raise result
        if kwargs.get("stream"):
            return _stream(result)
        return _Response(result)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("LOCAL_LLM_BASE_URL", "LOCAL_LLM_MODEL", "LOCAL_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(narrator_factory.settings, "local_llm_api_key", "", raising=False)
    yield


def _local_ready(monkeypatch, model: str = "qwen3.8-27b"):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8094/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", model)
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sk-local")
    monkeypatch.setattr(narrator_factory, "local_llm_reachable", lambda *a, **k: True)


def _cloud_config(**overrides) -> LLMConfig:
    defaults = dict(
        primary_provider=LLMProvider.DEEPSEEK,
        primary_model="deepseek-v4-flash",
    )
    defaults.update(overrides)
    return LLMConfig(**defaults)


# --------------------------------------------------------------------------- 
# refusal detection


def test_policy_choices_are_exactly_the_two_documented_values():
    assert POLICY_CHOICES == ("standard", "nsfw")


@pytest.mark.parametrize("text", [
    "I am sorry, but I cannot help with that.",
    "Im sorry, this request violates our usage policies.",
    "As an AI, I must decline.",
    "\u5f88\u62b1\u6b49\uff0c\u6211\u65e0\u6cd5\u6ee1\u8db3\u8fd9\u4e2a\u8bf7\u6c42\u3002",
])
def test_refusal_shapes_are_detected(text):
    assert _looks_like_refusal(text) is True


@pytest.mark.parametrize("text", [
    "The door groans open and cold air rushes in.",
    "I am sorry, but the rain kept falling. " * 20,
])
def test_real_narration_is_not_a_refusal(text):
    # Long prose is never a refusal even when the first sentence apologises:
    # narration is several paragraphs, a refusal is a sentence.
    assert _looks_like_refusal(text) is False


def test_empty_output_counts_as_a_refusal_for_the_signal_only():
    # An empty answer means the upstream had nothing to say, so the streaming
    # contract reports it as a refusal; the prose matcher itself stays honest.
    assert _looks_like_refusal("") is False
    assert LLMRouter(_cloud_config()).refusal_signal("   ") == REFUSAL_SIGNAL


def test_refusal_signal_is_none_for_clean_text():
    router = LLMRouter(_cloud_config())
    assert router.refusal_signal("The key turns in the lock.") is None
    assert router.refusal_signal("I cannot help with that.") == REFUSAL_SIGNAL
    assert router.refusal_signal("") == REFUSAL_SIGNAL


# --------------------------------------------------------------------------- 
# active routing


def test_standard_policy_keeps_the_primary_provider():
    router = LLMRouter(_cloud_config())
    assert router.with_content_policy(None) is router
    assert router.with_content_policy({"content_policy": "standard"}) is router


def test_nsfw_without_a_local_server_stays_on_the_primary(monkeypatch):
    router = LLMRouter(_cloud_config())
    assert router.local_route_available() is False
    assert router.with_content_policy({"content_policy": "nsfw"}) is router


def test_nsfw_routes_a_live_local_server_to_the_local_model(monkeypatch):
    _local_ready(monkeypatch)
    router = LLMRouter(_cloud_config())
    routed = router.with_content_policy({"content_policy": "nsfw"})
    assert routed is not router
    assert routed.config.primary_provider == LLMProvider.LOCAL
    assert routed.config.primary_model == "qwen3.8-27b"


def test_explicit_nsfw_provider_is_honoured_without_probing(monkeypatch):
    # An authored nsfw_model must work even when the default local probe fails.
    router = LLMRouter(_cloud_config(nsfw_provider="local", nsfw_model="abliterated"))
    routed = router.with_content_policy({"content_policy": "nsfw"})
    assert routed.config.primary_provider == LLMProvider.LOCAL
    assert routed.config.primary_model == "abliterated"


def test_complete_with_nsfw_policy_calls_the_local_model(monkeypatch):
    _local_ready(monkeypatch)
    recorder = _Recorder(lambda model: "Local words.")
    monkeypatch.setattr(llm_router.litellm, "acompletion", recorder)
    router = LLMRouter(_cloud_config())
    text = asyncio.run(
        router.complete([{"role": "user", "content": "hi"}], content_policy="nsfw")
    )
    assert text == "Local words."
    assert recorder.models == ["openai/qwen3.8-27b"]


def test_a_dead_local_server_degrades_to_the_primary(monkeypatch):
    _local_ready(monkeypatch)

    def behaviour(model):
        if model.startswith("openai/"):
            return RuntimeError("connection refused")
        return "Primary words."

    recorder = _Recorder(behaviour)
    monkeypatch.setattr(llm_router.litellm, "acompletion", recorder)
    router = LLMRouter(_cloud_config())
    text = asyncio.run(
        router.complete([{"role": "user", "content": "hi"}], content_policy="nsfw")
    )
    assert text == "Primary words."
    assert recorder.models == ["openai/qwen3.8-27b", "deepseek/deepseek-v4-flash"]


# --------------------------------------------------------------------------- 
# passive routing


def test_a_refusing_primary_is_retried_on_the_local_model(monkeypatch):
    _local_ready(monkeypatch)
    recorder = _Recorder(
        lambda model: (
            "I cannot help with that." if not model.startswith("openai/") else "Local words."
        )
    )
    monkeypatch.setattr(llm_router.litellm, "acompletion", recorder)
    router = LLMRouter(_cloud_config(local_fallback_on_refusal=True))
    text = asyncio.run(router.complete([{"role": "user", "content": "hi"}]))
    assert text == "Local words."
    assert recorder.models == ["deepseek/deepseek-v4-flash", "openai/qwen3.8-27b"]


def test_the_passive_reroute_needs_the_switch_and_a_live_local_server(monkeypatch):
    recorder = _Recorder(lambda model: "I cannot help with that.")
    monkeypatch.setattr(llm_router.litellm, "acompletion", recorder)
    router = LLMRouter(_cloud_config(local_fallback_on_refusal=True))
    text = asyncio.run(router.complete([{"role": "user", "content": "hi"}]))
    # No local server: the refusal is returned as-is rather than looping.
    assert text == "I cannot help with that."
    assert recorder.models == ["deepseek/deepseek-v4-flash"]


def test_stream_marks_a_refusal_instead_of_forwarding_it(monkeypatch):
    recorder = _Recorder(lambda model: "I cannot help with that.")
    monkeypatch.setattr(llm_router.litellm, "acompletion", recorder)
    router = LLMRouter(_cloud_config())

    async def collect():
        return [frame async for frame in router.stream([{"role": "user", "content": "hi"}])]

    assert asyncio.run(collect()) == [REFUSAL_SIGNAL]


def test_stream_forwards_clean_prose_unchanged(monkeypatch):
    recorder = _Recorder(lambda model: "The door opens.")
    monkeypatch.setattr(llm_router.litellm, "acompletion", recorder)
    router = LLMRouter(_cloud_config())

    async def collect():
        return [frame async for frame in router.stream([{"role": "user", "content": "hi"}])]

    assert "".join(asyncio.run(collect())) == "The door opens."


# --------------------------------------------------------------------------- 
# config hygiene


def test_unknown_provider_names_never_crash_the_router():
    config = _cloud_config(primary_provider="nonsense")
    assert config.primary_provider == LLMProvider.DEEPSEEK
    assert config.content_policy == "standard"
    assert config.local_fallback_on_refusal is False
