"""Narrator selection: template vs. model, and which model.

Two lanes need this answer — the structured engine lane (`/api/v1/.../actions`)
and the chat lane (`/api/game/action`) — and both used to decide it privately,
with different rules. This module is the single place that decides:

* `ENGINE_NARRATOR=template` (or `off`/`0`) forces the deterministic template.
* `ENGINE_NARRATOR=llm` forces the model.
* `auto` (the default) uses the model **when one is actually configured**.
  A local NInfer server counts: if `LOCAL_LLM_BASE_URL` and `LOCAL_LLM_API_KEY`
  are set, narration runs on the machine, with no cloud key at all.

The provider is `ENGINE_LLM_PROVIDER` when set, otherwise the local server if it
is configured, otherwise whatever `LLMConfig` defaults to.

Nothing here decides game state: a narrator that fails only produces
`narrative.status="failed"`, and the turn stays committed.
"""
from __future__ import annotations

import os
import socket
import time
import urllib.parse
from typing import Dict, Optional, Tuple

from app.config import settings
from app.services.narrative_service import LLMNarrator, TemplateNarrator

TEMPLATE_CHOICES = ("template", "off", "0", "none", "false")


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return str(default or "").strip()
    return str(value).strip()


def local_llm_base_url() -> str:
    return _env("LOCAL_LLM_BASE_URL", settings.local_llm_base_url)


def local_llm_model() -> str:
    return _env("LOCAL_LLM_MODEL", settings.local_llm_model) or "local"


def local_llm_api_key() -> str:
    return _env("LOCAL_LLM_API_KEY", settings.local_llm_api_key)


def local_llm_configured() -> bool:
    """A local server is usable only when we know where it is *and* its key."""
    return bool(local_llm_base_url() and local_llm_api_key())


PROBE_TTL_SECONDS = 30.0
PROBE_TIMEOUT = 0.35
_probe_cache: Dict[str, object] = {"url": "", "at": 0.0, "alive": False}


def _base_root() -> str:
    """`http://host:port` from the configured OpenAI-compatible base URL."""
    base = local_llm_base_url().rstrip("/")
    index = base.find("/v1")
    return base[:index] if index > 0 else base


def local_llm_reachable(timeout: float = PROBE_TIMEOUT) -> bool:
    """Cheap liveness probe for the local server.

    Configuration alone is not availability: the NInfer server may be down (or
    killed mid-session), and every turn would then spend the narrator timeout
    waiting for a refused connection. A probe keeps that from happening while
    still letting the model come back automatically. Successful probes are
    cached for `PROBE_TTL_SECONDS`; failures are not, so recovery is immediate.
    """
    base = local_llm_base_url()
    if not local_llm_configured():
        return False
    now = time.monotonic()
    if (
        _probe_cache.get("url") == base
        and float(_probe_cache.get("at") or 0) + PROBE_TTL_SECONDS > now
        and _probe_cache.get("alive")
    ):
        return True
    # Ask the socket directly: HTTP through urllib would cost ~700 ms even for
    # a refused connection, which is the same order as a real local generation.
    parsed = urllib.parse.urlsplit(_base_root())
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    alive = False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            alive = True
    except OSError:  # noqa: BLE001 - an unreachable model is not an error
        alive = False
    if alive:
        _probe_cache.update({"url": base, "at": now, "alive": True})
    else:
        _probe_cache.update({"url": base, "at": 0.0, "alive": False})
    return alive


def _cloud_configured(name: str) -> bool:
    if name not in ("deepseek", "openai", "anthropic"):
        return False
    if _env(f"{name.upper()}_API_KEY", getattr(settings, f"{name}_api_key", "")):
        return True
    if name in ("openai", "anthropic"):
        return bool(
            _env(f"{name.upper()}_PROXY_URL", getattr(settings, f"{name}_proxy_url", ""))
            and _env(f"{name.upper()}_PROXY_KEY", getattr(settings, f"{name}_proxy_key", ""))
        )
    return False


def _provider_name() -> str:
    from app.engines.llm_router import LLMConfig, LLMProvider

    explicit = _env("ENGINE_LLM_PROVIDER").lower()
    if explicit:
        return explicit
    if local_llm_reachable():
        return "local"
    default = LLMConfig().primary_provider.value
    for name in dict.fromkeys([default, *(p.value for p in LLMProvider)]):
        if _cloud_configured(name):
            return name
    return "default"


def narrator_choice(env_var: str = "ENGINE_NARRATOR") -> str:
    """`"llm"` or `"template"` for this lane."""
    from app.engines.llm_router import LLMConfig

    raw = _env(env_var, "auto").lower()
    if raw in TEMPLATE_CHOICES:
        return "template"
    if raw == "llm":
        return "llm"
    name = _provider_name()
    if name == "default":
        name = LLMConfig().primary_provider.value
    configured = local_llm_reachable() if name == "local" else _cloud_configured(name)
    return "llm" if configured else "template"


def prefer_local() -> bool:
    """True when this process should talk to the local server."""
    return _provider_name() == "local"


def build_router(provider: Optional[str] = None):
    """Build a supported provider's router; unknown names are configuration errors."""
    from app.engines.llm_router import LLMConfig, LLMProvider, LLMRouter

    name = (provider or _provider_name()).strip().lower()
    config = LLMConfig()
    if name != "default":
        selected = LLMProvider(name)
        models = {
            LLMProvider.DEEPSEEK: LLMConfig().primary_model,
            LLMProvider.OPENAI: "gpt-5.6-sol",
            LLMProvider.ANTHROPIC: "claude-sonnet-5",
            LLMProvider.LOCAL: local_llm_model(),
        }
        config.primary_provider = selected
        config.primary_model = models[selected]
    return LLMRouter(config)


def _template_context_builder(memory_provider=None):
    """Context builder for the deterministic lane; None when unavailable."""
    try:
        from app.engines.player_context import PlayerContextBuilder

        return PlayerContextBuilder(memory_provider=memory_provider)
    except Exception:  # noqa: BLE001 - the template narrator works without it
        return None


def build_agent_runtime(router=None, *, store=None, campaign_id: str = ""):
    """Agent roles on top of one router; None when no router is available."""
    if router is None:
        return None
    try:
        from app.engines.agent_router import build_agent_runtime as _build

        return _build(
            router,
            store=store if hasattr(store, "record_agent_call") else None,
            campaign_id=campaign_id,
        )
    except Exception:  # noqa: BLE001 - roles are an enhancement, never a gate
        return None


def build_narrator(
    env_var: str = "ENGINE_NARRATOR",
    *,
    language: str = "en",
    max_tokens: int = 600,
    temperature: float = 0.9,
    timeout: float = 45.0,
    store=None,
    campaign_id: str = "",
    memory_provider=None,
) -> Tuple[object, str]:
    """Return `(narrator, provider_name)`; never raises.

    Setup failure degrades to the template narrator: an unavailable model must
    not stop a player from taking a turn.
    """
    if narrator_choice(env_var) == "template":
        return TemplateNarrator(_template_context_builder(memory_provider)), "template"
    provider = _provider_name()
    try:
        router = build_router(provider)
    except Exception:  # noqa: BLE001 - narrator setup must never block play
        return TemplateNarrator(_template_context_builder(memory_provider)), "template"
    from app.engines.player_context import PlayerContextBuilder

    agents = build_agent_runtime(router, store=store, campaign_id=campaign_id)
    return (
        LLMNarrator(
            router,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            language=language or "en",
            agents=agents,
            context_builder=PlayerContextBuilder(memory_provider=memory_provider),
            memory_provider=memory_provider,
            campaign_id=campaign_id,
        ),
        provider,
    )


def describe() -> dict:
    """Read-only summary for diagnostics panels."""
    choice = narrator_choice()
    return {
        "choice": choice,
        "provider": _provider_name(),
        "local_configured": local_llm_configured(),
        "local_reachable": local_llm_reachable() if local_llm_configured() else False,
        "local_base_url": local_llm_base_url(),
        "local_model": local_llm_model(),
        "agents": _agent_summary(),
    }


def _agent_summary() -> dict:
    """Which role is pinned to which model, for the diagnostics panel.

    Only names and flags: keys never leave the process.
    """
    try:
        from app.engines.agent_router import load_agent_configs

        return {
            role: config.describe() for role, config in load_agent_configs().items()
        }
    except Exception:  # noqa: BLE001 - diagnostics must never break a request
        return {}
