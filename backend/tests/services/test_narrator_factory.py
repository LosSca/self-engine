"""Narrator selection rules (template vs. model, and which model)."""
from __future__ import annotations

import pytest

from app.services import narrator_factory
from app.services.narrative_service import LLMNarrator, TemplateNarrator


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "ENGINE_NARRATOR",
        "ENGINE_UNIFIED_NARRATOR",
        "ENGINE_LLM_PROVIDER",
        "LOCAL_LLM_BASE_URL",
        "LOCAL_LLM_MODEL",
        "LOCAL_LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(narrator_factory.settings, "local_llm_api_key", "", raising=False)
    for provider in ("deepseek", "openai", "anthropic"):
        for suffix in ("api_key", "proxy_url", "proxy_key"):
            name = f"{provider}_{suffix}"
            monkeypatch.delenv(name.upper(), raising=False)
            if hasattr(narrator_factory.settings, name):
                monkeypatch.setattr(narrator_factory.settings, name, "")
    yield


def _configure_local(monkeypatch, reachable: bool = True):
    """Configure a local server and pin its liveness.

    Liveness is a network fact, so tests never probe a real port: a healthy
    server is simulated by stubbing the probe.
    """
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8094/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3.8-27b")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sk-local")
    monkeypatch.setattr(narrator_factory, "local_llm_reachable", lambda *a, **k: reachable)


def test_nothing_configured_uses_the_template():
    assert narrator_factory.local_llm_configured() is False
    assert narrator_factory.narrator_choice() == "template"
    narrator, provider = narrator_factory.build_narrator()
    assert isinstance(narrator, TemplateNarrator)
    assert provider == "template"


def test_a_local_server_counts_as_configured(monkeypatch):
    _configure_local(monkeypatch)
    assert narrator_factory.local_llm_configured() is True
    assert narrator_factory.narrator_choice() == "llm"
    assert narrator_factory.prefer_local() is True


def test_auto_with_local_builds_a_local_narrator(monkeypatch):
    _configure_local(monkeypatch)
    narrator, provider = narrator_factory.build_narrator(language="zh", max_tokens=512)
    assert isinstance(narrator, LLMNarrator)
    assert provider == "local"
    assert narrator.language == "zh"
    assert narrator.max_tokens == 512
    # The router really points at the local OpenAI-compatible endpoint.
    from app.engines.llm_router import LLMProvider

    assert narrator.router.config.primary_provider == LLMProvider.LOCAL
    assert narrator.router.config.primary_model == "qwen3.8-27b"


@pytest.mark.parametrize("value", ["template", "off", "0", "none", "false", "TEMPLATE"])
def test_explicit_template_always_wins(monkeypatch, value):
    _configure_local(monkeypatch)
    monkeypatch.setenv("ENGINE_NARRATOR", value)
    narrator, provider = narrator_factory.build_narrator()
    assert isinstance(narrator, TemplateNarrator)
    assert provider == "template"


def test_explicit_llm_without_any_key_still_returns_a_model(monkeypatch):
    # An operator who forces llm gets a model object; a failing call degrades to
    # narrative.status="failed" at the action level, never to a crashed turn.
    monkeypatch.setenv("ENGINE_NARRATOR", "llm")
    narrator, provider = narrator_factory.build_narrator()
    assert isinstance(narrator, LLMNarrator)
    assert provider == "default"


def test_explicit_provider_overrides_the_local_default(monkeypatch):
    _configure_local(monkeypatch)
    monkeypatch.setenv("ENGINE_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    assert narrator_factory.prefer_local() is False
    narrator, provider = narrator_factory.build_narrator()
    assert isinstance(narrator, LLMNarrator)
    assert provider == "deepseek"


def test_lanes_are_configured_independently(monkeypatch):
    _configure_local(monkeypatch)
    monkeypatch.setenv("ENGINE_UNIFIED_NARRATOR", "template")
    assert narrator_factory.narrator_choice("ENGINE_NARRATOR") == "llm"
    assert narrator_factory.narrator_choice("ENGINE_UNIFIED_NARRATOR") == "template"


def test_a_configured_but_dark_server_falls_back_to_the_template(monkeypatch):
    # Configuration alone must not make every turn wait on a refused socket.
    _configure_local(monkeypatch, reachable=False)
    assert narrator_factory.local_llm_configured() is True
    assert narrator_factory.local_llm_reachable() is False
    assert narrator_factory.narrator_choice() == "template"
    assert narrator_factory.prefer_local() is False
    narrator, provider = narrator_factory.build_narrator()
    assert isinstance(narrator, TemplateNarrator)
    assert provider == "template"
    assert narrator_factory.describe()["local_reachable"] is False


def test_the_probe_is_cached_when_it_succeeds(monkeypatch):
    # No stubbed reachability here: this exercises the real probe path.
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8094/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sk-local")
    monkeypatch.setattr(
        narrator_factory, "_probe_cache", {"url": "", "at": 0.0, "alive": False}
    )
    connects = []

    class _Socket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_connect(address, timeout=None):
        connects.append((address, timeout))
        return _Socket()

    monkeypatch.setattr(narrator_factory.socket, "create_connection", fake_connect)
    assert narrator_factory.local_llm_reachable() is True
    assert narrator_factory.local_llm_reachable() is True
    assert connects == [(("127.0.0.1", 8094), narrator_factory.PROBE_TIMEOUT)], connects


def test_a_refused_port_is_reported_as_unreachable(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8094/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sk-local")
    monkeypatch.setattr(
        narrator_factory, "_probe_cache", {"url": "", "at": 0.0, "alive": False}
    )

    def fake_connect(address, timeout=None):
        raise ConnectionRefusedError("nobody home")

    monkeypatch.setattr(narrator_factory.socket, "create_connection", fake_connect)
    assert narrator_factory.local_llm_reachable() is False
    assert narrator_factory.narrator_choice() == "template"
    # Failures are not cached, so a server that comes back is picked up at once.
    assert narrator_factory.describe()["local_reachable"] is False


def test_describe_reports_the_local_endpoint(monkeypatch):
    _configure_local(monkeypatch)
    info = narrator_factory.describe()
    assert info["choice"] == "llm"
    assert info["provider"] == "local"
    assert info["local_model"] == "qwen3.8-27b"
    assert info["local_configured"] is True


def test_engine_route_caches_one_narrator_per_choice(monkeypatch):
    from app.api import routes_engine

    _configure_local(monkeypatch)
    routes_engine._narrator_cache.clear()
    first = routes_engine._get_narrator()
    second = routes_engine._get_narrator()
    assert first is second
    assert isinstance(first, LLMNarrator)
    routes_engine._narrator_cache.clear()


@pytest.mark.parametrize("name,model", [
    ("openai", "gpt-5.6-sol"),
    ("anthropic", "claude-sonnet-5"),
    ("deepseek", "deepseek-v4-flash"),
    ("local", "qwen3.8-27b"),
])
def test_explicit_provider_selects_matching_model(monkeypatch, name, model):
    _configure_local(monkeypatch)
    monkeypatch.setenv("ENGINE_LLM_PROVIDER", name)
    router = narrator_factory.build_router()
    assert router.config.primary_provider.value == name
    assert router.config.primary_model == model
    monkeypatch.setenv("ENGINE_LLM_PROVIDER", "local")
    assert narrator_factory.build_router(name.upper()).config.primary_provider.value == name


@pytest.mark.parametrize("name", ["openai", "anthropic", "deepseek"])
@pytest.mark.parametrize("source", ["environment", "settings"])
def test_auto_uses_configured_cloud_credentials(monkeypatch, name, source):
    _configure_local(monkeypatch, reachable=False)
    if source == "environment":
        monkeypatch.setenv(f"{name.upper()}_API_KEY", "test-key")
    else:
        monkeypatch.setattr(narrator_factory.settings, f"{name}_api_key", "test-key")
    narrator, provider = narrator_factory.build_narrator()
    assert isinstance(narrator, LLMNarrator)
    assert provider == name
    assert narrator.router.config.primary_provider.value == name


def test_auto_does_not_use_another_providers_key(monkeypatch):
    _configure_local(monkeypatch)
    monkeypatch.setenv("ENGINE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert narrator_factory.narrator_choice() == "template"


@pytest.mark.parametrize("name", ["openai", "anthropic"])
def test_auto_accepts_configured_supported_proxy(monkeypatch, name):
    monkeypatch.setenv(f"{name.upper()}_PROXY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv(f"{name.upper()}_PROXY_KEY", "test-key")
    assert narrator_factory.narrator_choice() == "llm"
    assert narrator_factory.build_router().config.primary_provider.value == name


def test_unknown_provider_is_not_silently_deepseek(monkeypatch):
    monkeypatch.setenv("ENGINE_LLM_PROVIDER", "typo")
    with pytest.raises(ValueError):
        narrator_factory.build_router()
    assert narrator_factory.narrator_choice() == "template"
    monkeypatch.setenv("ENGINE_NARRATOR", "llm")
    narrator, provider = narrator_factory.build_narrator()
    assert isinstance(narrator, TemplateNarrator)
    assert provider == "template"


def test_enabled_advisor_receives_the_router_directly(monkeypatch):
    from app.api import routes_engine

    router = object()
    monkeypatch.setenv("ENGINE_INTENT_ADVISOR", "1")
    monkeypatch.setattr(narrator_factory, "build_router", lambda: router)
    assert routes_engine._build_advisor().router is router
