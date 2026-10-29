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
    # The settings defaults point at a local server with an empty key, so the
    # "nothing configured" case is the ambient one.
    monkeypatch.setattr(narrator_factory.settings, "local_llm_api_key", "", raising=False)
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
    calls = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def open(self, request, timeout=None):
            calls.append(request.full_url)
            return _Response()

    monkeypatch.setattr(
        narrator_factory.urllib.request,
        "build_opener",
        lambda *a, **k: _Opener(),
    )
    assert narrator_factory.local_llm_reachable() is True
    assert narrator_factory.local_llm_reachable() is True
    assert len(calls) == 1, calls


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
