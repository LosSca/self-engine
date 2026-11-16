"""The recorded trail of a campaign: events, quest progress, model calls.

Separated from the lifecycle half (`routes_engine_campaigns`) only because the
single module had these routes interleaved with campaign state; the facade
registers this half after the action routes. Handlers are unchanged.
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
from app.api.routes_engine_actions import _service
from app.api.routes_engine_state import (
    QuestObjectiveView,
    QuestProgressResponse,
    QuestProgressView,
    _get_store,
    _require_debug_tools,
    _world_or_404,
)
from app.api.routes_engine_campaigns import (
    DomainEventView,
    RequestSummaryView,
    RequestRecordResponse,
    ProgressObjectiveBody,
    AgentCallView,
)
from app.services import authoring_session, interaction_planner, starter_worlds, world_service


router_events = APIRouter()
router_quest_progress = APIRouter()
router_requests = APIRouter()
router_trail = APIRouter()


@router_events.get("/campaigns/{campaign_id}/events", response_model=List[DomainEventView])
def list_campaign_events(
    campaign_id: str,
    limit: int = Query(50, ge=1, le=500),
    after_version: int = Query(0, ge=0),
):
    store = _get_store()
    try:
        return store.list_events(campaign_id, limit=limit, after_version=after_version)
    finally:
        store.close()


@router_quest_progress.get(
    "/campaigns/{campaign_id}/quest-progress", response_model=QuestProgressResponse
)
def get_quest_progress(campaign_id: str):
    """Quest definitions joined with this campaign's committed progress.

    Progress is read from the authoritative state; it is never recomputed from
    narration or from the request log.
    """
    game_store = _get_store()
    try:
        state = game_store.get_state(campaign_id)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=f"campaign '{campaign_id}' has no bound world",
            )
        world = _world_or_404(game_store, state.world_id)
    finally:
        game_store.close()

    quests: List[Dict[str, Any]] = []
    for quest in world.quests:
        progress = state.quests.get(quest.id)
        objectives = []
        for objective in quest.objectives:
            count = progress.objectives.get(objective.id, 0) if progress else 0
            objectives.append(
                {
                    "id": objective.id,
                    "kind": objective.kind,
                    "target": objective.target,
                    "description": objective.description,
                    "required_count": objective.required_count,
                    "count": count,
                    "done": count >= objective.required_count,
                }
            )
        quests.append(
            {
                "quest_id": quest.id,
                "title": quest.title,
                "description": quest.description,
                "enabled": quest.enabled,
                "status": progress.status if progress else "inactive",
                "objectives": objectives,
                "completed_objectives": sum(1 for item in objectives if item["done"]),
                "total_objectives": len(objectives),
                "rewards": [reward.model_dump(mode="json") for reward in quest.rewards],
            }
        )

    return {
        "campaign_id": campaign_id,
        "world_id": state.world_id,
        "version": state.version,
        "quests": quests,
        "active": [q["quest_id"] for q in quests if q["status"] == "active"],
        "completed": [q["quest_id"] for q in quests if q["status"] == "completed"],
        "failed": [q["quest_id"] for q in quests if q["status"] == "failed"],
    }


@router_quest_progress.post(
    "/campaigns/{campaign_id}/quests/{quest_id}/objectives/{objective_id}/progress",
    response_model=ActionResult,
)
async def progress_quest_objective(
    campaign_id: str,
    quest_id: str,
    objective_id: str,
    body: ProgressObjectiveBody,
):
    """Advance one objective as an author, through the normal commit path.

    This exists so the editor can demonstrate a quest without playing it out.
    It is a state change, so it is gated exactly like the other state-changing
    debug tools (403 in a production install), skips the rule check the player
    would have to pass, and is still written as one transactional commit plus
    domain events, which keeps the campaign's history and idempotency intact.
    """
    _require_debug_tools()
    game_store = _get_store()
    try:
        state = game_store.get_state(campaign_id)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail=f"campaign '{campaign_id}' has no bound world",
            )
        world = _world_or_404(game_store, state.world_id)
    finally:
        game_store.close()

    quest = next((q for q in world.quests if q.id == quest_id), None)
    if quest is None:
        raise HTTPException(status_code=404, detail=f"unknown quest '{quest_id}'")
    objective = next((o for o in quest.objectives if o.id == objective_id), None)
    if objective is None:
        raise HTTPException(
            status_code=404,
            detail=f"quest '{quest_id}' has no objective '{objective_id}'",
        )
    progress = state.quests.get(quest_id)
    if progress is not None and progress.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"quest '{quest_id}' is {progress.status}; only an active quest can advance",
        )

    from uuid import uuid4

    request_id = body.request_id.strip() or f"author-progress-{uuid4().hex[:12]}"
    service = _service()
    try:
        return await service.record_quest_progress(
            campaign_id,
            "quest_progress",
            f"{quest_id}:{objective_id}",
            request_id,
            {"amount": body.amount},
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        service.store.close()
        service.scenario_store.close()
@router_requests.get("/campaigns/{campaign_id}/requests", response_model=List[RequestSummaryView])
def list_campaign_requests(
    campaign_id: str,
    limit: int = Query(50, ge=1, le=500),
    after_version: int = Query(0, ge=0),
):
    """The turns this campaign has recorded, newest first.

    Read-only and cheap: it lists what each turn was and how it ended, so a
    debugger can then open one turn in full through `/actions/{request_id}`.
    """
    store = _get_store()
    try:
        return store.list_requests(campaign_id, limit=limit, after_version=after_version)
    finally:
        store.close()

# ------------------------------------------------------------- agent calls


@router_trail.get("/campaigns/{campaign_id}/agent-calls", response_model=List[AgentCallView])
def list_campaign_agent_calls(
    campaign_id: str,
    role: str = Query("", pattern="^$|^(narrative|system|memory)$"),
    limit: int = Query(100, ge=1, le=500),
):
    """Every agent call made for this campaign, newest first.

    Telemetry only: rows are written outside the game transaction, so a missing
    or failed write can never change what happened in the game.
    """
    store = _get_store()
    try:
        return store.list_agent_calls(campaign_id, limit=limit, role=role)
    finally:
        store.close()


@router_trail.get("/actions/{request_id}", response_model=RequestRecordResponse)
def get_action(request_id: str):
    store = _get_store()
    try:
        row = store.get_request(request_id)
    finally:
        store.close()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"unknown request_id '{request_id}'"
        )
    return row


# --------------------------------------------------------------- authoring
