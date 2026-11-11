"""Phase 1: agent roles resolve to their own model route, and never throw."""
from __future__ import annotations

import json
import time

import pytest

from app.engines.agent_router import (
    AGENT_ROLES,
    AgentConfig,
    AgentReply,
    AgentRuntime,
    build_agent_runtime,
    extract_json_object,
    load_agent_configs,
)
from app.engines.llm_router import LLMConfig, LLMProvider, LLMRouter


def _router(model: str = "base-model") -> LLMRouter:
    return LLMRouter(
        LLMConfig(primary_provider=LLMProvider.DEEPSEEK, primary_model=model)
    )


def test_roles_are_fixed_and_ordered():
    assert AGENT_ROLES == ("narrative", "system", "memory")


def test_no_role_is_configured_by_default():
    configs = load_agent_configs({})
    assert set(configs) == set(AGENT_ROLES)
    assert all(not config.configured for config in configs.values())
    assert all(config.enabled for config in configs.values())


def test_a_role_is_configured_only_with_provider_and_model():
    configs = load_agent_configs({
        "AGENTS_NARRATIVE_PROVIDER": "openai",
        "AGENTS_NARRATIVE_MODEL": "narrative-model",
        "AGENTS_NARRATIVE_TEMPERATURE": "0.4",
        "AGENTS_NARRATIVE_TIMEOUT": "12",
        "AGENTS_NARRATIVE_REASONING_EFFORT": "HIGH",
    })
    narrative = configs["narrative"]
    assert narrative.configured
    assert narrative.provider == LLMProvider.OPENAI
    assert narrative.model == "narrative-model"
    assert narrative.temperature == 0.4
    assert narrative.timeout == 12.0
    assert narrative.reasoning_effort == "high"
    assert not configs["system"].configured


def test_a_model_without_provider_is_not_a_route():
    configs = load_agent_configs({"AGENTS_SYSTEM_MODEL": "only-a-name"})
    assert not configs["system"].configured


def test_enabled_false_switches_a_role_off():
    configs = load_agent_configs({
        "AGENTS_SYSTEM_PROVIDER": "openai",
        "AGENTS_SYSTEM_MODEL": "m",
        "AGENTS_SYSTEM_ENABLED": "false",
    })
    assert configs["system"].enabled is False
    runtime = AgentRuntime(_router(), environ={
        "AGENTS_SYSTEM_PROVIDER": "openai",
        "AGENTS_SYSTEM_MODEL": "m",
        "AGENTS_SYSTEM_ENABLED": "false",
    })
    assert runtime.available("system") is False


@pytest.mark.asyncio
async def test_a_disabled_role_is_never_asked():
    asked = []

    async def fake_complete(router, messages, **kwargs):
        asked.append(router.config.primary_model)
        return "text"

    import app.engines.agent_router as module

    original = module._complete_with_timeout
    module._complete_with_timeout = fake_complete
    try:
        runtime = AgentRuntime(_router(), environ={
            "AGENTS_SYSTEM_ENABLED": "false",
            "AGENTS_NARRATIVE_ENABLED": "false",
        })
        reply = await runtime.ask("system", [{"role": "user", "content": "x"}])
    finally:
        module._complete_with_timeout = original
    assert asked == []
    assert reply.ok is False
    assert "disabled" in reply.error


def test_describe_never_leaks_a_key():
    config = load_agent_configs({
        "AGENTS_MEMORY_PROVIDER": "openai",
        "AGENTS_MEMORY_MODEL": "mem-model",
        "OPENAI_API_KEY": "sk-secret",
    })["memory"]
    described = config.describe()
    assert described["model"] == "mem-model"
    assert "sk-secret" not in json.dumps(described)


def test_router_for_returns_the_base_router_when_unconfigured():
    base = _router()
    runtime = AgentRuntime(base, environ={})
    router, provider = runtime.router_for("narrative")
    assert router is base
    assert provider == "deepseek"


def test_router_for_rewrites_only_the_primary_route():
    base = LLMRouter(LLMConfig(
        primary_provider=LLMProvider.DEEPSEEK,
        primary_model="base-model",
        fallback_provider=LLMProvider.LOCAL,
        fallback_model="local-model",
        temperature=0.9,
    ))
    runtime = AgentRuntime(base, environ={
        "AGENTS_NARRATIVE_PROVIDER": "openai",
        "AGENTS_NARRATIVE_MODEL": "narrative-model",
        "AGENTS_NARRATIVE_TEMPERATURE": "0.3",
    })
    router, provider = runtime.router_for("narrative")
    assert provider == "openai"
    assert router.config.primary_provider == LLMProvider.OPENAI
    assert router.config.primary_model == "narrative-model"
    assert router.config.temperature == 0.3
    # Everything the player configured about the base route survives.
    assert router.config.fallback_provider == LLMProvider.LOCAL
    assert router.config.fallback_model == "local-model"
    assert base.config.primary_model == "base-model"


@pytest.mark.asyncio
async def test_ask_returns_the_reply_and_records_it():
    recorded = []

    async def fake_complete(router, messages, **kwargs):
        return "hello"

    import app.engines.agent_router as module

    original = module._complete_with_timeout
    module._complete_with_timeout = fake_complete
    try:
        runtime = AgentRuntime(
            _router(), record=lambda **kw: recorded.append(kw), environ={}
        )
        reply = await runtime.ask("narrative", [{"role": "user", "content": "hi"}], task="narrate")
    finally:
        module._complete_with_timeout = original
    assert reply.ok
    assert reply.text == "hello"
    assert recorded and recorded[0]["role"] == "narrative"
    assert recorded[0]["status"] == "ok"
    assert recorded[0]["task"] == "narrate"


@pytest.mark.asyncio
async def test_ask_degrades_before_it_gives_up():
    calls = []

    async def fake_complete(router, messages, **kwargs):
        calls.append(router.config.primary_model)
        if router.config.primary_model == "system-model":
            raise RuntimeError("system down")
        return "degraded answer"

    import app.engines.agent_router as module

    original = module._complete_with_timeout
    module._complete_with_timeout = fake_complete
    try:
        runtime = AgentRuntime(_router(), environ={
            "AGENTS_SYSTEM_PROVIDER": "deepseek",
            "AGENTS_SYSTEM_MODEL": "system-model",
            "AGENTS_NARRATIVE_PROVIDER": "deepseek",
            "AGENTS_NARRATIVE_MODEL": "narrative-model",
        })
        reply = await runtime.ask("system", [{"role": "user", "content": "classify"}])
    finally:
        module._complete_with_timeout = original
    assert calls == ["system-model", "narrative-model"]
    assert reply.ok
    assert reply.degraded is True
    assert "system" in reply.attempts[0]


@pytest.mark.asyncio
async def test_ask_never_raises_when_every_role_fails():
    async def fake_complete(router, messages, **kwargs):
        raise RuntimeError("all down")

    import app.engines.agent_router as module

    original = module._complete_with_timeout
    module._complete_with_timeout = fake_complete
    try:
        runtime = AgentRuntime(_router(), environ={})
        reply = await runtime.ask("narrative", [{"role": "user", "content": "hi"}])
    finally:
        module._complete_with_timeout = original
    assert isinstance(reply, AgentReply)
    assert reply.ok is False
    assert "all down" in reply.error


@pytest.mark.asyncio
async def test_ask_without_fallback_stops_at_the_first_failure():
    async def fake_complete(router, messages, **kwargs):
        raise RuntimeError("down")

    import app.engines.agent_router as module

    original = module._complete_with_timeout
    module._complete_with_timeout = fake_complete
    try:
        runtime = AgentRuntime(_router(), environ={})
        reply = await runtime.ask("system", [{"role": "user", "content": "x"}], fallback=False)
    finally:
        module._complete_with_timeout = original
    assert reply.ok is False
    assert reply.attempts == ["system:error"]


def test_extract_json_object_accepts_the_usual_shapes():
    payload = {"kind": "Location", "name": "Abbey"}
    assert extract_json_object(json.dumps(payload)) == payload
    assert extract_json_object("\x60\x60\x60json\n" + json.dumps(payload) + "\n\x60\x60\x60") == payload
    assert extract_json_object("Sure: " + json.dumps(payload) + " hope that helps") == payload


def test_extract_json_object_rejects_garbage():
    assert extract_json_object("no json here") is None
    assert extract_json_object("[1, 2, 3]") is None
    assert extract_json_object("") is None
    assert extract_json_object("{not valid}") is None


def test_build_agent_runtime_wires_the_recorder_when_the_store_has_it():
    class Store:
        def record_agent_call(self, **kwargs):
            return kwargs

    class Bare:
        pass

    runtime = build_agent_runtime(_router(), store=Store(), campaign_id="c1")
    assert runtime is not None
    assert runtime.campaign_id == "c1"
    wired = build_agent_runtime(_router(), store=Bare())
    assert wired is not None


def test_agent_reply_reports_ok():
    assert AgentReply(role="narrative", text="x").ok is True
    assert AgentReply(role="narrative", text="", error="boom").ok is False
