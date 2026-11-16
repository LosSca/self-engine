"""`POST /campaigns/{campaign_id}/world`: bind a world to a campaign.

Everything else that used to live in `routes_engine`'s campaign block moved to
`routes_engine_state` (state, snapshots, export/import),
`routes_engine_campaigns_trail` (the recorded trail) or stayed in
`routes_engine_actions`. Only this one route has to be registered before the
campaign state routes, so it keeps a module of its own.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.engines.action_registry import all_actions, get_action
from app.models.game import (
    ActionResult,
    ActionRequest,
    CustomActionDef,
    DoorDef,
    Effect,
    EntityDef,
    GameEventDef,
    GameState,
    ItemDef,
    LocationDef,
    ObjectiveDef,
    QuestDef,
    WorldDefinition,
)
from app.api.routes_engine_state import _get_store
from app.services import authoring_session, interaction_planner, starter_worlds, world_service


class DomainEventView(BaseModel):
    """One committed domain event (the fact log for a campaign).

    `id` is the store's own row key, kept as an opaque string so it stays
    meaningful whichever backend wrote it; `state_version` is the number the
    event is ordered by.
    """

    id: str = ""
    campaign_id: str = ""
    request_id: str = ""
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    state_version: int = 0
    created_at: str = ""


class BindWorldRequest(BaseModel):
    world_id: str = Field(..., min_length=1)
    reset: bool = False


class RequestSummaryView(BaseModel):
    """One recorded turn, index form: enough to pick one out of a list."""

    request_id: str
    campaign_id: str = ""
    status: str = ""
    state_version: int = 0
    created_at: str = ""


class RequestRecordResponse(BaseModel):
    """GET /actions/{request_id}: one recorded turn, as the store saw it.

    The request and result payloads stay opaque on purpose - they are whatever
    the caller sent and the service returned for that turn, including the
    refusal code, the applied effects and the narrative outcome.
    """

    request_id: str
    campaign_id: str = ""
    request: Dict[str, Any] = Field(default_factory=dict)
    status: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)
    state_version: int = 0
    created_at: str = ""


# --------------------------------------------------------------------- worlds


class ProgressObjectiveBody(BaseModel):
    """An author or debugger asserting that one objective advanced."""

    amount: int = Field(1, ge=1, le=100)
    request_id: str = Field(
        "", max_length=128, description="replays of the same id are not applied twice"
    )


class AgentCallView(BaseModel):
    role: str
    task: str = ""
    provider: str = ""
    model: str = ""
    status: str = "ok"
    degraded: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    error: str = ""
    created_at: str = ""


router_bind = APIRouter()


@router_bind.post("/campaigns/{campaign_id}/world", response_model=GameState)
def bind_world(campaign_id: str, body: BindWorldRequest):
    store = _get_store()
    try:
        try:
            return world_service.bind_world(
                store, campaign_id, body.world_id, reset=body.reset
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        store.close()
