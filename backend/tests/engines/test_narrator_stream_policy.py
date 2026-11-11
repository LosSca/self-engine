from __future__ import annotations
import asyncio
import pytest

from app.engines import narrator_engine
from app.engines.llm_router import REFUSAL_SIGNAL, LLMConfig


class FakeRouter:
    def __init__(self, frames, local=None, available=True):
        self.frames = list(frames)
        self.local = local
        self.available = available
        self.calls = 0
        self.config = LLMConfig(local_fallback_on_refusal=True)

    async def stream(self, messages=None, **kwargs):
        self.calls += 1
        for frame in self.frames:
            yield frame

    def local_route_available(self):
        return self.available

    def with_content_policy(self, options):
        assert options == {"content_policy": "nsfw"}
        return self.local


def _engine(router):
    return narrator_engine.NarratorEngine(router)


async def _collect(agen):
    return [chunk async for chunk in agen]


def test_refusal_signal_is_never_forwarded():
    router = FakeRouter([REFUSAL_SIGNAL])
    frames = asyncio.run(_collect(_engine(router)._stream_with_policy_retry([])))
    assert REFUSAL_SIGNAL not in "".join(frames)


def test_refusal_retries_once_on_the_local_route():
    local = FakeRouter(["The door swings open."])
    router = FakeRouter([REFUSAL_SIGNAL], local=local)
    frames = asyncio.run(_collect(_engine(router)._stream_with_policy_retry([])))
    assert frames == ["The door swings open."]
    assert local.calls == 1


def test_normal_stream_does_not_touch_the_local_route():
    local = FakeRouter(["unused"])
    router = FakeRouter(["Rain falls."], local=local)
    frames = asyncio.run(_collect(_engine(router)._stream_with_policy_retry([])))
    assert frames == ["Rain falls."]
    assert local.calls == 0


def test_disabled_fallback_leaves_the_stream_empty():
    local = FakeRouter(["unused"])
    router = FakeRouter([REFUSAL_SIGNAL], local=local)
    router.config = LLMConfig(local_fallback_on_refusal=False)
    frames = asyncio.run(_collect(_engine(router)._stream_with_policy_retry([])))
    assert frames == []
    assert local.calls == 0


def test_unreachable_local_leaves_the_stream_empty():
    local = FakeRouter(["unused"])
    router = FakeRouter([REFUSAL_SIGNAL], local=local, available=False)
    frames = asyncio.run(_collect(_engine(router)._stream_with_policy_retry([])))
    assert frames == []
    assert local.calls == 0


def test_local_refusal_is_also_swallowed():
    local = FakeRouter([REFUSAL_SIGNAL])
    router = FakeRouter([REFUSAL_SIGNAL], local=local)
    frames = asyncio.run(_collect(_engine(router)._stream_with_policy_retry([])))
    assert frames == []
