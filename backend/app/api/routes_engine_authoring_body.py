"""The second half of the authoring routes.

Split out of `routes_engine_authoring` so the facade can register it after the
campaign routes rather than before them; the handlers and their models are
unchanged. Everything shared with `router_head` is imported from there.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.config import settings
from app.db.game_state_store import GameStateStore, StateConflictError
from app.engines.action_registry import all_actions, get_action
from app.engines.event_engine import diff_states, simulate_event, validate_event
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
from app.services import authoring_session, interaction_planner, starter_worlds, world_service
from app.engines.agent_router import AGENT_ROLES, load_agent_configs

from app.api.routes_engine_state import _world_or_404
from app.api.routes_engine_authoring import (
    ValidateResponse,
    _world_or_404_detail,
    AuthoringProposeBody,
    AuthoringOpView,
    AuthoringChangeView,
    AuthoringProposalResponse,
    AuthoringCommitBody,
    AuthoringCommitResponse,
    _authoring_proposal,
    RoomPlanResponse,
    PreviewChainResponse,
    PreviewActionsBody,
    RequirementBody,
    RequirementResponse,
    _plan_state,
    _edit_world,
    LocalModelStatus,
    _get_store,
    _get_scenario_store,
    get_world,
)


router_body = APIRouter()


@router_body.post("/worlds/{world_id}/authoring/propose", response_model=AuthoringProposalResponse)
async def propose_world_edits(world_id: str, body: AuthoringProposeBody):
    """Turn one sentence into staged, validated edits. Writes nothing."""
    from app.services import ai_generator

    store = _get_store();
    try:
        world = _world_or_404(store, world_id)
        scenario_store = _get_scenario_store()
        try:
            existing = scenario_store.list_knowledge(world.scenario_id) if world.scenario_id else []
        finally:
            scenario_store.close()
    finally:
        store.close()
    router = ai_generator.router_for(body.preset_id or None)
    if router is None:
        raise HTTPException(
            status_code=503,
            detail="未配置可用模型：请先设置供应商密钥或本地模型",
        )
    history = [{"role": turn.role, "text": turn.text} for turn in body.history]
    try:
        reply, proposal = await authoring_session.propose_edits(
            router, body.message, world, existing, history, body.language
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="模型超时，未产生任何提案")
    ops = [AuthoringOpView(**op) for op in proposal.get("ops") or []]
    return AuthoringProposalResponse(
        reply=reply,
        world_id=world.world_id,
        ops=ops,
        changes=[AuthoringChangeView(**row) for row in proposal.get("changes") or []],
        knowledge_titles=list(proposal.get("knowledge_titles") or []),
        counts=dict(proposal.get("world_summary") or {}),
        warnings=list(proposal.get("warnings") or []),
        empty=bool(proposal.get("empty")),
    )


@router_body.post("/worlds/{world_id}/authoring/commit", response_model=AuthoringCommitResponse)
def commit_world_edits(world_id: str, body: AuthoringCommitBody):
    """Write a proposal the author approved. Never touches a campaign state."""
    draft = _authoring_proposal(body.proposal)
    store = _get_store();
    try:
        world = _world_or_404(store, world_id)
        if draft["world"] is not None and draft["world"].world_id != world.world_id:
            raise HTTPException(
                status_code=400,
                detail=f"proposal targets world '{draft['world'].world_id}', not '{world.world_id}'",
            )
        scenario_store = _get_scenario_store()
        try:
            result = authoring_session.commit_proposal(
                draft, world, scenario_store, store, world.scenario_id
            )
        finally:
            scenario_store.close()
    finally:
        store.close()
    return AuthoringCommitResponse(**result)


# -------------------------------------------------------------- interaction


@router_body.post("/worlds/{world_id}/plan", response_model=RoomPlanResponse)
def plan_world_room(world_id: str, campaign_id: str = Query("")):
    """Every action possible in this room, decided by the rules engine.

    Read-only.  Without a ``campaign_id`` the plan is computed against a fresh
    starting state, which is what an editor wants while authoring: "on turn one,
    what can a player do in this room, and why not the rest?"
    """
    store = _get_store()
    try:
        world = _world_or_404(store, world_id)
        state = _plan_state(store, world, campaign_id)
    finally:
        store.close()
    return interaction_planner.plan_room(world, state)


@router_body.post("/worlds/{world_id}/preview-actions", response_model=PreviewChainResponse)
def preview_world_actions(world_id: str, body: PreviewActionsBody):
    """Replay a chain of actions on a copy: what would each step change?

    Nothing is committed.  ``simulated`` is always true and ``start_version`` is
    the version the chain started from, so the console can label the rows as
    speculative and never confuse them with a real turn in the event log.
    """
    store = _get_store()
    try:
        world = _world_or_404(store, world_id)
        state = _plan_state(store, world, body.campaign_id)
    finally:
        store.close()
    try:
        return interaction_planner.run_preview(
            world, state, [step.model_dump() for step in body.steps]
        )
    except interaction_planner.PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router_body.post("/worlds/{world_id}/explain", response_model=RequirementResponse)
def explain_world_action(world_id: str, body: RequirementBody):
    """Why an action is blocked, and how far away the fix is.

    Read-only.  Only requirements the player can satisfy by walking and picking
    up are planned; everything else is returned in ``unplannable`` instead of
    being guessed at.
    """
    store = _get_store()
    try:
        world = _world_or_404(store, world_id)
        state = _plan_state(store, world, body.campaign_id)
    finally:
        store.close()
    return interaction_planner.explain_requirement(
        world, state, body.action, body.target
    )

# -------------------------------------------------------------------- events


@router_body.post(
    "/worlds/{world_id}/events/{event_id}/validate", response_model=ValidateResponse
)
def validate_event_config(world_id: str, event_id: str):
    store = _get_store()
    try:
        try:
            world = world_service.get_world(store, world_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()
    event = next((e for e in world.events if e.id == event_id), None)
    if event is None:
        raise HTTPException(status_code=404, detail=f"unknown event '{event_id}'")
    errors = validate_event(world, event)
    return ValidateResponse(valid=not errors, errors=errors)


@router_body.post("/worlds/{world_id}/events/{event_id}/simulate")
def simulate_event_config(
    world_id: str,
    event_id: str,
    campaign_id: Optional[str] = Query(None),
):
    store = _get_store()
    try:
        try:
            world = world_service.get_world(store, world_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        state = store.get_state(campaign_id) if campaign_id else None
        if state is None:
            state = world_service.build_initial_state(world, campaign_id or "__preview__")
        return simulate_event(world, state, event_id)
    finally:
        store.close()


# -------------------------------------------------------------- world editor


@router_body.put("/worlds/{world_id}/locations/{location_id}", response_model=WorldDefinition)
def upsert_location(world_id: str, location_id: str, body: LocationDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_location(world, body, location_id))


@router_body.delete("/worlds/{world_id}/locations/{location_id}", response_model=WorldDefinition)
def delete_location(world_id: str, location_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_location(world, location_id))


@router_body.put("/worlds/{world_id}/items/{item_id}", response_model=WorldDefinition)
def upsert_item(world_id: str, item_id: str, body: ItemDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_item(world, body, item_id))


@router_body.delete("/worlds/{world_id}/items/{item_id}", response_model=WorldDefinition)
def delete_item(world_id: str, item_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_item(world, item_id))


@router_body.put("/worlds/{world_id}/doors/{door_id}", response_model=WorldDefinition)
def upsert_door(world_id: str, door_id: str, body: DoorDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_door(world, body, door_id))


@router_body.delete("/worlds/{world_id}/doors/{door_id}", response_model=WorldDefinition)
def delete_door(world_id: str, door_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_door(world, door_id))


@router_body.put("/worlds/{world_id}/entities/{entity_id}", response_model=WorldDefinition)
def upsert_entity(world_id: str, entity_id: str, body: EntityDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_entity(world, body, entity_id))


@router_body.delete("/worlds/{world_id}/entities/{entity_id}", response_model=WorldDefinition)
def delete_entity(world_id: str, entity_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_entity(world, entity_id))


@router_body.put("/worlds/{world_id}/events/{event_id}", response_model=WorldDefinition)
def upsert_event(world_id: str, event_id: str, body: GameEventDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_event(world, body, event_id))


@router_body.delete("/worlds/{world_id}/events/{event_id}", response_model=WorldDefinition)
def delete_event(world_id: str, event_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_event(world, event_id))


@router_body.put("/worlds/{world_id}/quests/{quest_id}", response_model=WorldDefinition)
def upsert_quest(world_id: str, quest_id: str, body: QuestDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_quest(world, body, quest_id))


@router_body.delete("/worlds/{world_id}/quests/{quest_id}", response_model=WorldDefinition)
def delete_quest(world_id: str, quest_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_quest(world, quest_id))


@router_body.get("/worlds/{world_id}/quests", response_model=List[QuestDef])
def list_world_quests(world_id: str):
    """Read-only quest definitions of one world, in authored order."""
    game_store = _get_store()
    try:
        return _world_or_404(game_store, world_id).quests
    finally:
        game_store.close()


@router_body.post(
    "/worlds/{world_id}/quests", response_model=WorldDefinition, status_code=201
)
def create_quest(world_id: str, body: QuestDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_quest(world, body))


@router_body.get("/worlds/{world_id}/quests/{quest_id}", response_model=QuestDef)
def get_world_quest(world_id: str, quest_id: str):
    game_store = _get_store()
    try:
        world = _world_or_404(game_store, world_id)
    finally:
        game_store.close()
    quest = next((item for item in world.quests if item.id == quest_id), None)
    if quest is None:
        raise HTTPException(status_code=404, detail=f"unknown quest '{quest_id}'")
    return quest


@router_body.patch(
    "/worlds/{world_id}/quests/{quest_id}", response_model=WorldDefinition
)
def patch_quest(world_id: str, quest_id: str, body: QuestDef):
    """Replace one quest; the path id is authoritative, never the body's."""
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.upsert_quest(world, body, quest_id)
    )


@router_body.put(
    "/worlds/{world_id}/quests/{quest_id}/objectives/{objective_id}",
    response_model=WorldDefinition,
)
def upsert_quest_objective(
    world_id: str, quest_id: str, objective_id: str, body: ObjectiveDef
):
    from app.services import world_editor

    return _edit_world(
        world_id,
        lambda world: world_editor.upsert_objective(world, quest_id, body, objective_id),
    )


@router_body.delete(
    "/worlds/{world_id}/quests/{quest_id}/objectives/{objective_id}",
    response_model=WorldDefinition,
)
def delete_quest_objective(world_id: str, quest_id: str, objective_id: str):
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.delete_objective(world, quest_id, objective_id)
    )


@router_body.put(
    "/worlds/{world_id}/quests/{quest_id}/rewards/{reward_id}",
    response_model=WorldDefinition,
)
def upsert_quest_reward(world_id: str, quest_id: str, reward_id: str, body: Effect):
    """One reward row; the path id is the effect kind (rewards carry no id)."""
    from app.services import world_editor

    return _edit_world(
        world_id,
        lambda world: world_editor.upsert_reward(world, quest_id, body, reward_id),
    )


@router_body.delete(
    "/worlds/{world_id}/quests/{quest_id}/rewards/{reward_id}",
    response_model=WorldDefinition,
)
def delete_quest_reward(world_id: str, quest_id: str, reward_id: str):
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.delete_reward(world, quest_id, reward_id)
    )


@router_body.put(
    "/worlds/{world_id}/custom-actions/{action_id}", response_model=WorldDefinition
)
def upsert_custom_action(world_id: str, action_id: str, body: CustomActionDef):
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.upsert_custom_action(world, body, action_id)
    )


@router_body.delete(
    "/worlds/{world_id}/custom-actions/{action_id}", response_model=WorldDefinition
)
def delete_custom_action(world_id: str, action_id: str):
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.delete_custom_action(world, action_id)
    )


# ---------------------------------------------------------------- local model


@router_body.get("/local-model", response_model=LocalModelStatus)
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
