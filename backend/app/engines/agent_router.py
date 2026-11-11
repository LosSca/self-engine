"""Role-based model routing for the two core agents (Phase 1).

The engine talks to models by *role* ("narrative", "system", "memory"), never by
model name. Which provider/model answers a role is configuration, so swapping a
model never touches a business service:

    AGENTS_SYSTEM_MODEL=...          model that answers the system role
    AGENTS_SYSTEM_PROVIDER=...       optional provider override
    AGENTS_SYSTEM_ENABLED=0          disable the role entirely
    AGENTS_SYSTEM_TIMEOUT=45         seconds per call
    AGENTS_SYSTEM_MAX_TOKENS=1200
    AGENTS_SYSTEM_TEMPERATURE=0
    AGENTS_SYSTEM_REASONING_EFFORT=high

Every role defaults to the existing router, so a deployment that never heard of
roles keeps its current behaviour. The actual request still goes through
LLMRouter.complete, which owns proxies, caching headers, retries and refusal
handling: this module only decides *which* router answers and what it may spend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.engines.llm_router import LLMConfig, LLMProvider, LLMRouter, _as_provider

logger = logging.getLogger(__name__)

#: Roles the engine can ask for. Adding one means adding a config prefix.
AGENT_ROLES = ("narrative", "system", "memory")

#: Degradation chain used when a role is unusable or its call failed. The
#: narrative role is the historical default, so it answers last.
DEGRADE_TO = {
    "system": ("narrative",),
    "narrative": (),
    "memory": ("narrative",),
}


def _read(env: Dict[str, str], name: str) -> Optional[str]:
    """Read one setting from the caller's mapping.

    Role configuration is resolved per call through a mapping, never through
    `os.environ` directly: a process-wide read would make a role impossible to
    pin in a test, and would ignore an explicit override.
    """
    value = env.get(name)
    return None if value is None else str(value)


def _env_flag(env: Dict[str, str], name: str, default: bool = True) -> bool:
    raw = _read(env, name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_float(env: Dict[str, str], name: str, default: float) -> float:
    raw = _read(env, name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring invalid %s=%r", name, raw)
        return default


def _env_int(env: Dict[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("ignoring invalid %s=%r", name, raw)
        return default


def _env_effort(env: Dict[str, str], name: str) -> str:
    """Reasoning effort for one role. Empty means "provider default"."""
    raw = (_read(env, name) or "").strip().lower()
    return raw if raw in ("low", "medium", "high") else ""


@dataclass
class AgentConfig:
    """One role's resolved model and budget."""

    role: str
    enabled: bool = True
    provider: LLMProvider | None = None
    model: str = ""
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout: float = 45.0
    reasoning_effort: str = ""

    @property
    def configured(self) -> bool:
        """True when this role has a model of its own (not the primary fallback)."""
        return bool(self.provider and self.model)

    def describe(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "enabled": self.enabled,
            "provider": self.provider.value if self.provider else "",
            "model": self.model,
            "timeout": self.timeout,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


@dataclass
class AgentReply:
    """The text one agent produced, plus who produced it and at what cost."""

    role: str
    text: str
    provider: str = ""
    model: str = ""
    elapsed_ms: int = 0
    #: True when the answer did not come from the role's own model (fallback,
    #: disabled, or a swallowed failure with empty text).
    degraded: bool = False
    error: str = ""
    attempts: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text.strip()) and not self.error


def load_agent_configs(environ: Optional[Dict[str, str]] = None) -> Dict[str, AgentConfig]:
    """Read every role's configuration from the environment.

    Read per call (cheap) so an operator can change a role's model without a
    restart, and so tests can set the environment per test.
    """
    env = os.environ if environ is None else environ
    configs: Dict[str, AgentConfig] = {}
    for role in AGENT_ROLES:
        prefix = f"AGENTS_{role.upper()}_"
        provider = _as_provider((env.get(prefix + "PROVIDER") or "").strip() or None, None)
        raw_temperature = env.get(prefix + "TEMPERATURE")
        temperature_value: Optional[float] = None
        if raw_temperature not in (None, ""):
            try:
                temperature_value = float(raw_temperature)
            except (TypeError, ValueError):
                logger.warning(
                    "ignoring invalid %s=%r", prefix + "TEMPERATURE", raw_temperature
                )
        configs[role] = AgentConfig(
            role=role,
            enabled=_env_flag(env, prefix + "ENABLED", True),
            provider=provider,
            model=(env.get(prefix + "MODEL") or "").strip(),
            temperature=temperature_value,
            max_tokens=_env_int(env, prefix + "MAX_TOKENS", 0) or None,
            timeout=_env_float(env, prefix + "TIMEOUT", 45.0),
            reasoning_effort=_env_effort(env, prefix + "REASONING_EFFORT"),
        )
    return configs


class AgentRuntime:
    """Ask a role for an answer; log it; degrade instead of raising.

    The runtime never lets a model failure escape: callers get an AgentReply
    with ``error`` set and empty text, so a caller can fall back to template
    prose without a game turn being affected. It also records every call through
    ``record`` (optional) so the debug console can show what each agent was
    asked and why it failed.
    """

    def __init__(
        self,
        router: Optional[LLMRouter] = None,
        *,
        record: Optional[Callable[..., Any]] = None,
        environ: Optional[Dict[str, str]] = None,
        campaign_id: str = "",
    ):
        self.router = router
        self.record = record
        self._environ = environ
        self.campaign_id = campaign_id

    # ------------------------------------------------------------- resolving
    def configs(self) -> Dict[str, AgentConfig]:
        return load_agent_configs(self._environ)

    def config(self, role: str) -> AgentConfig:
        configs = self.configs()
        return configs.get(role) or configs["narrative"]

    def router_for(self, role: str) -> tuple[Optional[LLMRouter], str]:
        """Return (router, provider value) that answers ``role``.

        An unconfigured role reuses the existing router untouched, which keeps
        the historical behaviour (and its proxy/cache handling) for free.
        """
        config = self.config(role)
        if self.router is None:
            return None, ""
        if not config.provider or not config.model:
            return self.router, self.router.config.primary_provider.value
        source = self.router.config
        if source is None:
            # A router without a configuration (a test double, a custom
            # adapter) answers for the role itself: there is nothing to clone
            # and nothing to override.
            return self.router, (config.provider.value if config.provider else "")
        overridden = LLMConfig(
            primary_provider=config.provider,
            primary_model=config.model,
            orchestrator_model=None,
            fallback_provider=source.fallback_provider,
            fallback_model=source.fallback_model,
            temperature=(
                config.temperature if config.temperature is not None else source.temperature
            ),
            max_tokens=config.max_tokens or source.max_tokens,
            content_policy=source.content_policy,
            nsfw_provider=source.nsfw_provider,
            nsfw_model=source.nsfw_model,
            local_fallback_on_refusal=source.local_fallback_on_refusal,
            local_provider=source.local_provider,
            local_model=source.local_model,
        )
        return LLMRouter(overridden), config.provider.value

    # ---------------------------------------------------------------- asking
    def available(self, role: str) -> bool:
        return bool(self.config(role).enabled) and self.router is not None

    async def ask(
        self,
        role: str,
        messages: List[Dict[str, Any]],
        *,
        task: str = "",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        policy: str = "",
        fallback: bool = True,
    ) -> AgentReply:
        """Ask ``role`` (then its degradation chain) and never raise."""
        config = self.config(role)
        if not config.enabled:
            return AgentReply(
                role=role, text="", error=f"agent role '{role}' is disabled", degraded=True
            )
        if self.router is None:
            return AgentReply(
                role=role, text="", error="no model router configured", degraded=True
            )
        chain: List[str] = [role]
        if fallback:
            chain.extend(DEGRADE_TO.get(role, ()))
        attempts: List[str] = []
        last_error = ""
        for index, current in enumerate(chain):
            current_config = self.config(current)
            if not current_config.enabled:
                attempts.append(f"{current}:disabled")
                continue
            router, provider_name = self.router_for(current)
            if router is None:
                attempts.append(f"{current}:no-router")
                continue
            model_name = getattr(router.config, "primary_model", "")
            started = time.monotonic()
            try:
                text = await _complete_with_timeout(
                    router,
                    messages,
                    timeout=current_config.timeout,
                    max_tokens=(
                        max_tokens or current_config.max_tokens or router.config.max_tokens
                    ),
                    temperature=(
                        temperature
                        if temperature is not None
                        else current_config.temperature
                    ),
                    policy=policy,
                )
            except Exception as exc:  # noqa: BLE001 - degrade, never raise
                last_error = f"{type(exc).__name__}: {exc}"
                attempts.append(f"{current}:error")
                logger.warning(
                    "agent call failed for role %s (%s): %s", current, task, last_error
                )
                self._record(
                    current,
                    task=task,
                    provider=provider_name,
                    model=model_name,
                    status="error",
                    degraded=index > 0,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=last_error,
                )
                continue
            elapsed_ms = int((time.monotonic() - started) * 1000)
            attempts.append(f"{current}:ok")
            self._record(
                current,
                task=task,
                provider=provider_name,
                model=model_name,
                status="ok",
                degraded=index > 0,
                elapsed_ms=elapsed_ms,
            )
            return AgentReply(
                role=current,
                text=text or "",
                provider=provider_name,
                model=model_name,
                elapsed_ms=elapsed_ms,
                degraded=index > 0,
                attempts=attempts,
            )
        return AgentReply(
            role=role,
            text="",
            error=last_error or "no usable agent role",
            degraded=True,
            attempts=attempts,
        )

    # ------------------------------------------------------------- recording
    def _record(self, role: str, **kwargs: Any) -> None:
        if self.record is None:
            return
        try:
            self.record(role=role, campaign_id=self.campaign_id, **kwargs)
        except Exception:  # noqa: BLE001 - observability is best effort
            logger.warning("agent call record failed for %s", role, exc_info=True)


async def _complete_with_timeout(
    router: LLMRouter,
    messages: List[Dict[str, Any]],
    *,
    timeout: float,
    max_tokens: int,
    temperature: Optional[float],
    policy: str = "",
) -> str:
    kwargs: Dict[str, Any] = {"max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = float(temperature)
    if policy:
        kwargs["content_policy"] = policy
    return await asyncio.wait_for(router.complete(messages, **kwargs), timeout=timeout)


_JSON_FENCE = re.compile("(?s)`{3}(?:json)?\\s*(.*?)`{3}")


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON object extraction from a model answer.

    Models wrap JSON in prose or fences; anything that is not an object is
    treated as "no answer", never as a partial result.
    """
    if not text or not text.strip():
        return None
    candidates: List[str] = list(_JSON_FENCE.findall(text))
    candidates.append(text)
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate.strip())
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def build_agent_runtime(
    router: Optional[LLMRouter], store: Any = None, campaign_id: str = ""
) -> AgentRuntime:
    """Wire an AgentRuntime to a store's ``record_agent_call`` when available."""
    record = None
    if store is not None and hasattr(store, "record_agent_call"):
        record = store.record_agent_call
    return AgentRuntime(router, record=record, campaign_id=campaign_id)
