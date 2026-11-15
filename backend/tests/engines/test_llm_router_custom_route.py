"""A custom OpenAI-compatible vendor is reachable without touching the code."""

import importlib
import sys

import pytest

from app.engines import llm_router


@pytest.fixture(autouse=True)
def isolated_prefs_db(tmp_path, monkeypatch):
    """Point the credential/prefs store at a temp file.

    CredentialStore defaults to the *real* scenarios.db in the backend
    directory, and this test writes a row to it. One test against a
    developer's database is already too many, and in a sandbox that denies the
    write the test fails for a reason that has nothing to do with routing.
    """
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))


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
    monkeypatch.delenv("CUSTOM_LLM_BASE_URL", raising=False)
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

    monkeypatch.delenv("CUSTOM_LLM_BASE_URL", raising=False)
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


def test_a_keyless_endpoint_is_still_a_route(monkeypatch):
    """A local Ollama or an intranet gateway has no key at all."""
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_KEY", "")
    monkeypatch.setenv("CUSTOM_LLM_BASE_URL", "http://127.0.0.1:11434/v1")

    assert llm_router.custom_route_available() is True
    router = llm_router.LLMRouter(llm_router.LLMConfig())
    # litellm always attaches a bearer header; empty would send "Bearer ".
    assert router._get_api_key(llm_router.LLMProvider.OPENAI) == "none"


def test_a_route_saved_from_the_settings_panel_is_seen_without_a_restart(monkeypatch):
    """The panel writes os.environ; the module-level value was frozen at import."""
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_URL", "")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_KEY", "")
    monkeypatch.setattr(llm_router, "_CUSTOM_LLM_MODEL", "")
    monkeypatch.delenv("CUSTOM_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("CUSTOM_LLM_MODEL", raising=False)

    from app.db.credential_store import CredentialStore, apply_credentials

    store = CredentialStore()
    try:
        store.put(
            "custom",
            api_key="sk-later",
            base_url="https://vendor.example/v2",
            model="vendor-model-2",
        )
    finally:
        store.close()
    apply_credentials()

    assert llm_router.custom_route_available() is True
    assert llm_router.custom_default_model() == "vendor-model-2"
