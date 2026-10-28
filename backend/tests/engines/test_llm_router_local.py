"""Local NInfer provider: routed through litellm's OpenAI transport.

These tests do not touch a real server: they pin the wiring (base URL, bearer key,
model alias, native streaming, context window) so the local engine can be swapped
in without changing any caller.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.engines.llm_router as llm_router
from app.engines.llm_router import LLMConfig, LLMProvider, LLMRouter

LOCAL_URL = "http://127.0.0.1:8094/v1"
LOCAL_KEY = "sk-ninfer-test-key"
LOCAL_MODEL = "qwen3.8-27b"


@pytest.fixture
def router():
    return LLMRouter(
        LLMConfig(
            primary_provider=LLMProvider.LOCAL,
            primary_model=LOCAL_MODEL,
            temperature=0.85,
            max_tokens=512,
        )
    )


@pytest.fixture(autouse=True)
def local_settings(monkeypatch):
    monkeypatch.setattr(llm_router, "_LOCAL_LLM_URL", LOCAL_URL)
    monkeypatch.setattr(llm_router, "_LOCAL_LLM_KEY", LOCAL_KEY)
    monkeypatch.setattr(llm_router, "_LOCAL_CONTEXT_WINDOW", 49152)


def _response(text: str) -> MagicMock:
    return MagicMock(choices=[MagicMock(message=MagicMock(content=text))])


class _AsyncStream:
    def __init__(self, parts):
        self._parts = parts

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for part in self._parts:
            chunk = MagicMock()
            chunk.choices = [MagicMock(delta=MagicMock(content=part))]
            yield chunk


def test_local_is_a_provider_value():
    assert LLMProvider("local") == LLMProvider.LOCAL


def test_local_model_string_uses_openai_transport(router):
    assert router._build_model_string(LLMProvider.LOCAL, LOCAL_MODEL) == "openai/qwen3.8-27b"


def test_local_api_base_and_key(router):
    assert router._get_api_base(LLMProvider.LOCAL) == LOCAL_URL
    assert router._get_api_key(LLMProvider.LOCAL) == LOCAL_KEY


@pytest.mark.asyncio
async def test_complete_posts_to_local_server(router):
    mock = AsyncMock(return_value=_response("The door swings open."))
    with patch("app.engines.llm_router.litellm.acompletion", new=mock):
        result = await router.complete([{"role": "user", "content": "describe"}], max_tokens=64)

    assert result == "The door swings open."
    kwargs = mock.call_args.kwargs
    assert kwargs["model"] == "openai/qwen3.8-27b"
    assert kwargs["api_base"] == LOCAL_URL
    assert kwargs["api_key"] == LOCAL_KEY
    assert kwargs["max_tokens"] == 64
    # NInfer is strictly OpenAI-compatible: no reasoning_effort leaking through.
    assert "reasoning_effort" not in kwargs
    # Unsupported params are dropped instead of failing the call.
    assert kwargs.get("drop_params") is True


@pytest.mark.asyncio
async def test_stream_uses_native_sse_with_local_server(router):
    mock = AsyncMock(return_value=_AsyncStream(["You ", "unlock ", "it."]))
    with patch("app.engines.llm_router.litellm.acompletion", new=mock):
        parts = [chunk async for chunk in router.stream([{"role": "user", "content": "go"}])]

    assert "".join(parts) == "You unlock it."
    kwargs = mock.call_args.kwargs
    assert kwargs["stream"] is True
    assert kwargs["api_base"] == LOCAL_URL
    assert kwargs["model"] == "openai/qwen3.8-27b"


def test_missing_local_key_fails_with_readable_error(router, monkeypatch):
    monkeypatch.setattr(llm_router, "_LOCAL_LLM_KEY", "")
    with pytest.raises(RuntimeError, match="LOCAL_LLM_API_KEY"):
        router._get_api_key(LLMProvider.LOCAL)


@pytest.mark.asyncio
async def test_missing_key_blocks_the_call_before_the_network(router, monkeypatch):
    monkeypatch.setattr(llm_router, "_LOCAL_LLM_KEY", "")
    mock = AsyncMock()
    with patch("app.engines.llm_router.litellm.acompletion", new=mock):
        with pytest.raises(RuntimeError, match="LOCAL_LLM_API_KEY"):
            await router.complete([{"role": "user", "content": "hi"}])
    mock.assert_not_called()


def test_local_context_window_comes_from_server_launch(router):
    assert router.config.get_context_window() == 49152
