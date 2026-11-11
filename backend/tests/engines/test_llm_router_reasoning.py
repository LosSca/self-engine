"""Regression tests for provider-specific request parameters.

DeepSeek's API answers an unsupported optional parameter with a hard error:

    litellm.exceptions.UnsupportedParamsError: deepseek does not support
    parameters: ['reasoning_effort']

Because narrative calls pass reasoning=False by default, sending the flag at all
broke every story beat for DeepSeek users. Reasoning is off by default on that
provider, so the parameter is simply not forwarded.
"""

from app.engines.llm_router import LLMProvider, _reasoning_kwargs


def test_deepseek_never_receives_reasoning_effort():
    assert _reasoning_kwargs(LLMProvider.DEEPSEEK, False) == {}


def test_openai_keeps_explicit_disable():
    assert _reasoning_kwargs(LLMProvider.OPENAI, False) == {"reasoning_effort": "none"}


def test_anthropic_uses_thinking_flag():
    assert _reasoning_kwargs(LLMProvider.ANTHROPIC, False) == {"thinking": {"type": "disabled"}}


def test_local_server_forwards_nothing():
    assert _reasoning_kwargs(LLMProvider.LOCAL, False) == {}


def test_reasoning_enabled_forwards_nothing_anywhere():
    for provider in LLMProvider:
        assert _reasoning_kwargs(provider, True) == {}


def test_sampling_kwargs_are_provider_aware(monkeypatch):
    """The fallback retry path used the primary model string for capability checks."""
    from app.engines import llm_router as mod

    router = mod.LLMRouter.__new__(mod.LLMRouter)  # no network/config side effects
    router.config = type("C", (), {"temperature": 0.7, "primary_provider": LLMProvider.OPENAI})()

    primary = router._sampling_kwargs("openai/gpt-4o-mini")
    fallback = router._sampling_kwargs("deepseek/deepseek-v4-flash", LLMProvider.DEEPSEEK)

    assert "drop_params" not in primary
    assert fallback.get("drop_params") is True
