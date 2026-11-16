"""The two /api/v1 routes that do not belong to any of the four groups.

`GET /agents` and `GET /local-model` were interleaved with campaign routes in
the original single module, and the published route table still has them
there. They live here so the facade can register each one at the exact position
that keeps that order unchanged.
"""
from __future__ import annotations

from __future__ import annotations

import os

from fastapi import APIRouter
from app.api.routes_engine_authoring import AgentRoleView, AgentRolesResponse, LocalModelStatus
from app.config import settings
from app.engines.agent_router import AGENT_ROLES, load_agent_configs


router_tail = APIRouter()



@router_tail.get("/agents", response_model=AgentRolesResponse)
def list_agent_roles():
    """Which model answers each agent role.

    Read-only and key-free: this is what the debug panel shows so an author can
    confirm that a role is routed where they think it is. Secrets never appear
    here; an unconfigured role simply inherits the primary model.
    """
    configs = load_agent_configs()
    roles = [configs[role] for role in AGENT_ROLES if role in configs]
    return AgentRolesResponse(
        roles=[
            AgentRoleView(
                role=config.role,
                enabled=config.enabled,
                configured=config.configured,
                provider=(config.provider.value if config.provider else ""),
                model=config.model,
                timeout=config.timeout,
                max_tokens=config.max_tokens or 0,
                temperature=config.temperature,
                reasoning_effort=config.reasoning_effort,
            )
            for config in roles
        ],
        note="AGENTS_<ROLE>_PROVIDER / AGENTS_<ROLE>_MODEL configure a role; unset inherits the primary model.",
    )


router_end = APIRouter()


@router_end.get("/local-model", response_model=LocalModelStatus)
async def local_model_status():
    """Probe the configured local OpenAI-compatible server (NInfer).

    Read-only: lists /v1/models so the editor can show whether the local engine
    is up and which alias it serves, without sending a generation request.
    """
    from app.config import settings

    base_url = settings.local_llm_base_url.rstrip("/")
    api_key = settings.local_llm_api_key or os.environ.get("LOCAL_LLM_API_KEY", "")
    status = LocalModelStatus(
        configured=bool(base_url),
        base_url=base_url,
        model=settings.local_llm_model,
        api_key_set=bool(api_key),
        reachable=False,
    )
    if not base_url:
        status.detail = "LOCAL_LLM_BASE_URL is empty"
        return status
    try:
        import httpx

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
    except Exception as exc:  # noqa: BLE001 - diagnostic endpoint reports, never raises
        status.detail = f"{type(exc).__name__}: {exc}"
        return status
    if resp.status_code != 200:
        status.detail = f"HTTP {resp.status_code}: {resp.text[:300]}"
        return status
    try:
        payload = resp.json()
    except ValueError:
        status.detail = "response was not JSON"
        return status
    status.reachable = True
    status.available_models = [
        str(m.get("id", "")) for m in payload.get("data", []) if isinstance(m, dict)
    ]
    return status
