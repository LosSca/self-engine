"""A custom OpenAI-compatible vendor is reachable without touching the code."""

import importlib
import sys

import pytest

from app.engines import llm_router


@pytest.fixture()
def custom_env(monkeypatch):
    monkeypatch.setenv("CUSTOM_LLM_BASE_URL", "https://vendor.example/v1")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "sk-test-key")
    monkeypatch.setenv("CUSTOM_LLM_MODEL", "vendor-model-1")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "https://vendor.example/v1")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_KEY", "sk-test-key")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_MODEL", "vendor-model-1")
    return monkeypatch


def test_without_configuration_the_route_is_absent(monkeypatch):
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_KEY", "")
    assert llm_router.custom_route_available() is False


def test_a_base_url_and_key_enable_the_route(custom_env):
    assert llm_router.custom_route_available() is True


def test_openai_uses_the_custom_base_url_and_key(custom_env):
    from app.engines.llm_router import LLMConfig, LLMRouter, LLMProvider

    router = LLMRouter(LLMConfig())
    assert router._get_api_base(LLMProvider.OPENAI) == "https://vendor.example/v1"
    assert router._get_api_key(LLMProvider.OPENAI) == "sk-test-key"


def test_a_custom_endpoint_gets_the_openai_transport_prefix(custom_env):
    """litellm needs "openai/<id>" so it posts the bare id to api_base."""
    from app.engines.llm_router import LLMConfig, LLMRouter, LLMProvider

    router = LLMRouter(LLMConfig())
    assert router._build_model_string(LLMProvider.OPENAI, "vendor-model-1") == "openai/vendor-model-1"


def test_without_a_custom_endpoint_openai_keeps_its_own_prefix(monkeypatch):
    from app.engines.llm_router import LLMConfig, LLMRouter, LLMProvider

    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_KEY", "")
    monkeypatch.setattr(llm_router, "_OPENAI_PROXY_URL", "")
    router = LLMRouter(LLMConfig())
    assert router._build_model_string(LLMProvider.OPENAI, "gpt-5.6-sol") == "openai/gpt-5.6-sol"
    assert router._get_api_base(LLMProvider.OPENAI) is None


def test_the_chosen_model_serves_both_roles(custom_env):
    """There is no auxiliary tier to guess for somebody else's catalogue."""
    from app.api import routes_game
    from app.engines.llm_router import LLMProvider

    routes_game.apply_model_policy(LLMProvider.OPENAI, "vendor-model-2")
    config = routes_game._llm.config
    assert config.primary_provider == LLMProvider.OPENAI
    assert config.orchestrator_model == "vendor-model-2"
    assert config.primary_model == "vendor-model-2"


def test_the_custom_model_survives_a_later_unrelated_call(custom_env):
    from app.api import routes_game
    from app.engines.llm_router import LLMProvider

    routes_game.apply_model_policy(LLMProvider.OPENAI, "vendor-model-3")
    routes_game.apply_model_policy(LLMProvider.DEEPSEEK, "deepseek-v4-flash")
    routes_game.apply_model_policy(LLMProvider.OPENAI, "")
    assert routes_game._llm.config.orchestrator_model == "vendor-model-3"
