"""The /api/v1 surface for authoring worlds, plus the world model itself.

Registered in two passes by `app/api/routes_engine.py`: `router_head` (world
presets, worlds, evolution, the map/events views) first, then `router_body`
(the authoring studio and the per-world sub-resources) after the campaign
routes. That split exists only because the original single module had them in
that order and route matching is order-sensitive. The models and helpers both
halves share stay here, in one module.
"""
from __future__ import annotations

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.config import settings
from app.db.game_state_store import GameStateStore, StateConflictError
from app.db.scenario_store import ScenarioStore
from app.api.routes_engine_state import _world_or_404
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
from app.api.routes_engine_actions import ActionSpecView


class StarterWorldPresetView(BaseModel):
    """A `world_type` an author can pick to start a campaign inside the rules lane."""

    world_type: str
    name: str
    summary: str
    locations: int
    items: int
    doors: int
    quests: int


def _preset_summary(key: str) -> StarterWorldPresetView:
    document = starter_worlds.build_world_document("", f"preset_{key}", language="en", world_type=key)
    return StarterWorldPresetView(
        world_type=key,
        name=document.get("name", key),
        summary=document.get("description", ""),
        locations=len(document.get("locations") or []),
        items=len(document.get("items") or []),
        doors=len(document.get("doors") or []),
        quests=len(document.get("quests") or []),
    )


class ValidateResponse(BaseModel):
    valid: bool
    errors: List[str] = Field(default_factory=list)


class GenerateWorldRequest(BaseModel):
    scenario_id: str = ""
    prompt: str = Field(..., min_length=1, max_length=4000)
    preset_id: str = ""
    language: str = "中文"


class WorldSummary(BaseModel):
    """One row of GET /worlds (a saved world, not a campaign)."""

    world_id: str
    name: str = ""
    description: str = ""
    scenario_id: str = ""
    start_location_id: str = ""
    locations: int = 0
    items: int = 0
    doors: int = 0
    entities: int = 0
    events: int = 0
    quests: int = 0
    updated_at: str = ""
    created_at: str = ""


class WorldWriteResponse(BaseModel):
    status: str
    world_id: str
    name: str = ""
    scenario_id: str = ""
    locations: int = 0
    items: int = 0
    doors: int = 0
    entities: int = 0
    events: int = 0
    quests: int = 0
    updated_at: str = ""
    created_at: str = ""


class _MapRef(BaseModel):
    id: str
    name: str = ""
    known: bool = True


class MapExit(BaseModel):
    target_id: str
    label: str = ""
    door: Optional[Dict[str, Any]] = None
    requires_item: Optional[_MapRef] = None


class MapOccupant(BaseModel):
    id: str
    name: str = ""
    kind: str = "npc"
    known: bool = True


class MapRegion(BaseModel):
    id: str
    locations: List[str] = Field(default_factory=list)


class MapLocation(BaseModel):
    id: str
    name: str = ""
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    region: str = ""
    x: Optional[int] = None
    y: Optional[int] = None
    is_start: bool = False
    exits: List[MapExit] = Field(default_factory=list)
    items: List[_MapRef] = Field(default_factory=list)
    npcs: List[MapOccupant] = Field(default_factory=list)
    doors: List[Dict[str, Any]] = Field(default_factory=list)


class MapCounts(BaseModel):
    locations: int = 0
    items: int = 0
    doors: int = 0
    entities: int = 0
    quests: int = 0
    events: int = 0


class WorldMapResponse(BaseModel):
    """GET /worlds/{id}/map: a projection, never authoritative run-time state."""

    world_id: str
    scenario_id: str = ""
    name: str = ""
    description: str = ""
    start_location_id: str = ""
    regions: List[MapRegion] = Field(default_factory=list)
    locations: List[MapLocation] = Field(default_factory=list)
    items: List[Dict[str, Any]] = Field(default_factory=list)
    doors: List[Dict[str, Any]] = Field(default_factory=list)
    entities: List[Dict[str, Any]] = Field(default_factory=list)
    counts: MapCounts = Field(default_factory=MapCounts)


class AuthorScenarioRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    scenario_id: str = ""
    title: str = Field("", max_length=120)
    preset_id: str = ""
    language: str = "中文"
    location_count: int = Field(6, ge=3, le=12)
    #: Persist what came back.  Validation still runs when False, which makes
    #: this a dry run of the authoring pipeline.
    save: bool = True


class AuthorScenarioResponse(BaseModel):
    status: str
    scenario_id: str = ""
    scenario_title: str = ""
    world_id: str = ""
    world_saved: bool = False
    storyline_saved: bool = False
    knowledge_ids: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    counts: Dict[str, int] = Field(default_factory=dict)


class EvolutionStatementView(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    kind: str = ""
    confidence: float = 0.5
    entities: List[str] = Field(default_factory=list)


class EvolutionProposeRequest(BaseModel):
    text: str = Field("", max_length=8000)
    statements: List[EvolutionStatementView] = Field(default_factory=list)
    campaign_id: str = ""
    observer: str = "player"
    source: str = ""
    auto_commit: Optional[float] = None
    #: Write the validated draft back. False makes this a safe dry run, which
    #: is what the editor shows while an author is still deciding.
    commit: bool = False


class EvolutionProposalView(BaseModel):
    id: str
    kind: str
    statement: str
    confidence: float
    status: str
    origin: str = ""
    source: str = ""
    observer: str = ""
    canonical: bool = False
    personal: bool = False
    payload: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)
    error: str = ""
    result: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class EvolutionResponse(BaseModel):
    world_id: str
    campaign_id: str = ""
    proposals: List[EvolutionProposalView] = Field(default_factory=list)
    committed: List[str] = Field(default_factory=list)
    pending: List[str] = Field(default_factory=list)
    rejected: List[str] = Field(default_factory=list)
    conflicts: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    degraded: bool = False
    error: str = ""


def _commit_reviewed(
    store: Any, write_world: Any, world: Any, row: Dict[str, Any]
) -> Any:
    """Commit exactly the proposal a human approved, not newer ones."""
    from app.services import world_evolution as evo

    # A human approved this one: confidence is the model's hesitation, not the
    # reviewer's, so eligibility here is the canonical check alone.
    service = evo.WorldEvolutionService(store, persist=write_world, auto_commit=0.0)
    proposal = _proposal_from_row(row)
    report = evo.EvolutionReport.empty(
        world_id=str(row.get("world_id") or ""),
        campaign_id=str(row.get("campaign_id") or ""),
    )
    proposal.status = evo.STATUS_PENDING
    report.proposals.append(proposal)
    if not service._eligible(proposal):
        proposal.status = evo.STATUS_REJECTED
        proposal.error = "not committable: kind={0} confidence={1}".format(
            proposal.kind, round(proposal.confidence, 2)
        )
        report.rejected.append(proposal.id)
        service.record(report)
        return report
    draft, applied, warnings = service._stage(world, [proposal])
    report.warnings.extend(warnings)
    if not applied:
        proposal.status = evo.STATUS_REJECTED
        proposal.error = warnings[0] if warnings else "draft did not change"
        report.rejected.append(proposal.id)
        service.record(report)
        return report
    report = service.commit(report, world, draft=draft, applied=applied)
    if proposal.status == evo.STATUS_AUTO:
        proposal.status = evo.STATUS_COMMITTED
    service.record(report)
    return report


def _evolution_service(store: Any, write_world: Any, auto_commit: Optional[float]) -> Any:
    """Build the service around the world writer, never around a store slot."""
    from app.services import world_evolution

    kwargs: Dict[str, Any] = {"persist": write_world}
    if auto_commit is not None:
        kwargs["auto_commit"] = auto_commit
    return world_evolution.WorldEvolutionService(store, **kwargs)


def _world_writer(game_store: Any) -> Any:
    """Persist a validated draft through the same path the editor uses."""

    def write(world: Any) -> None:
        world_service.save_world(game_store, world)

    return write


def _evolution_statements(body: EvolutionProposeRequest) -> List[Any]:
    """Classify free text deterministically; explicit statements win."""
    from app.engines import system_agent

    if body.statements:
        return [
            system_agent.Statement(
                text=item.text,
                kind=item.kind or "canonical_fact",
                confidence=item.confidence,
                entities=list(item.entities),
            )
            for item in body.statements
        ]
    if not body.text.strip():
        return []
    return list(system_agent.classify_locally(body.text).statements)


def _evolution_response(report: Any) -> EvolutionResponse:
    return EvolutionResponse(
        world_id=report.world_id,
        campaign_id=report.campaign_id,
        proposals=[EvolutionProposalView(**item) for item in report.to_dict()["proposals"]],
        committed=list(report.committed),
        pending=list(report.pending),
        rejected=list(report.rejected),
        conflicts=list(report.conflicts),
        warnings=list(report.warnings),
        degraded=report.degraded,
        error=report.error,
    )


def _proposal_from_row(row: Dict[str, Any]) -> Any:
    from app.services import world_evolution

    return world_evolution.EvolutionProposal(
        id=row.get("id", ""),
        kind=row.get("kind", ""),
        statement=row.get("statement", ""),
        confidence=row.get("confidence", 0.5),
        world_id=row.get("world_id", ""),
        campaign_id=row.get("campaign_id", ""),
        origin=row.get("origin", ""),
        source=row.get("source", ""),
        observer=row.get("observer", ""),
        status=row.get("status", "pending"),
        payload=row.get("payload") or {},
        evidence=row.get("evidence") or [],
        warnings=row.get("warnings") or [],
        error=row.get("error", ""),
        result=row.get("result") or {},
        created_at=row.get("created_at", ""),
    )


def _proposal_view(row: Dict[str, Any]) -> Dict[str, Any]:
    from app.services import world_evolution as evo

    kind = evo.normalize_kind(row.get("kind", ""))
    return {
        "id": row.get("id", ""),
        "kind": kind,
        "statement": row.get("statement", ""),
        "confidence": float(row.get("confidence") or 0.0),
        "status": row.get("status", "pending"),
        "origin": row.get("origin", ""),
        "source": row.get("source", ""),
        "observer": row.get("observer", ""),
        "canonical": evo.is_canonical(kind),
        "personal": evo.is_personal(kind),
        "payload": row.get("payload") or {},
        "warnings": row.get("warnings") or [],
        "error": row.get("error", ""),
        "result": row.get("result") or {},
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }


def _campaign_entities(world_id: str, campaign_id: Optional[str]):
    """Run-time entity records for one campaign, or {} when unavailable."""
    if not campaign_id:
        return {}
    store = _get_store()
    try:
        state = store.get_state(campaign_id)
    finally:
        store.close()
    if state is None or state.world_id != world_id:
        return {}
    return dict(state.entities)


def _world_or_404_detail(world_id: str):
    """Load a world for read-only projection, mapping LookupError to 404."""
    store = _get_store()
    try:
        return world_service.get_world(store, world_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


class AgentRoleView(BaseModel):
    role: str
    enabled: bool
    configured: bool
    provider: str = ""
    model: str = ""
    timeout: float = 0.0
    max_tokens: int = 0
    temperature: Optional[float] = None
    reasoning_effort: str = ""


class AgentRolesResponse(BaseModel):
    roles: List[AgentRoleView]
    note: str = ""


class AuthoringTurn(BaseModel):
    """One message of the authoring conversation."""

    role: str = Field("user", pattern="^(user|assistant)$")
    text: str = Field("", max_length=4000)


class AuthoringProposeBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: List[AuthoringTurn] = Field(default_factory=list, max_length=40)
    preset_id: str = Field("", max_length=64)
    language: str = Field("中文", max_length=16)


class AuthoringOpView(BaseModel):
    kind: str
    summary: str = ""


class AuthoringChangeView(BaseModel):
    label: str
    before: int = 0
    after: int = 0


class AuthoringProposalResponse(BaseModel):
    """A staged edit proposal. Nothing has been written yet."""

    status: str = "proposed"
    reply: str = ""
    world_id: str = ""
    ops: List[AuthoringOpView] = []
    changes: List[AuthoringChangeView] = []
    knowledge_titles: List[str] = []
    counts: Dict[str, int] = {}
    warnings: List[str] = []
    empty: bool = False


class AuthoringCommitBody(BaseModel):
    proposal: Dict[str, Any]
    expected_world_id: str = Field("", max_length=64)


class AuthoringCommitResponse(BaseModel):
    status: str = "committed"
    world_saved: bool = False
    world_id: str = ""
    knowledge_ids: List[str] = []
    warnings: List[str] = []


def _authoring_proposal(body: Any) -> Any:
    """Validate a reviewed proposal back into the draft the planner produced.

    The client round-trips the proposal, so it must not be trusted: the world
    part is re-parsed as a WorldDefinition and re-checked for references before
    anything is written.
    """
    from app.models.game import WorldDefinition

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="proposal must be an object")
    world_payload = body.get("world")
    draft = None
    if world_payload is not None:
        try:
            draft = WorldDefinition.model_validate(world_payload)
        except Exception as exc:  # noqa: BLE001 - client supplied
            raise HTTPException(status_code=400, detail=f"proposal world is invalid: {exc}")
    entries = []
    for item in body.get("knowledge") or []:
        try:
            entries.append(KnowledgeEntryCreate.model_validate(item))
        except Exception as exc:  # noqa: BLE001 - client supplied
            raise HTTPException(status_code=400, detail=f"proposal knowledge is invalid: {exc}")
    return {"world": draft, "knowledge": entries}


class PlanRowView(BaseModel):
    """One candidate action and how the rules engine judged it."""

    action: str
    target: str
    label: str = ""
    ok: bool = False
    code: str = ""
    message: str = ""


class RoomPlanResponse(BaseModel):
    campaign_id: str = ""
    world_id: str = ""
    location_id: str = ""
    version: int = 0
    allowed: List[PlanRowView] = []
    blocked: List[PlanRowView] = []


class PreviewStepResult(BaseModel):
    """What one previewed step would do, as decided by the rules engine."""

    step: int
    action: str
    target: str = ""
    params: Dict[str, Any] = {}
    ok: bool = False
    code: str = ""
    message: str = ""
    effects: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    changes: List[Dict[str, Any]] = []
    effect_errors: List[str] = []
    advanced: bool = False


class PreviewChainResponse(BaseModel):
    campaign_id: str = ""
    world_id: str = ""
    start_version: int = 0
    steps: List[PreviewStepResult] = []
    stopped_at: int = 0
    simulated: bool = True
    step_limit: int = 0


class PreviewStep(BaseModel):
    """One authored step of a previewed action chain."""

    action: str = Field(..., min_length=1, max_length=32)
    target: str = Field("", max_length=64)
    params: Dict[str, Any] = Field(default_factory=dict)


class PreviewActionsBody(BaseModel):
    steps: List[PreviewStep] = Field(..., min_length=1, max_length=6)
    campaign_id: str = Field("", max_length=128)


class RequirementBody(BaseModel):
    action: str = Field(..., min_length=1, max_length=32)
    target: str = Field(..., min_length=1, max_length=64)
    campaign_id: str = Field("", max_length=128)


class RequirementTarget(BaseModel):
    kind: str = ""
    target: str = ""
    label: str = ""


class RequirementRow(BaseModel):
    kind: str = ""
    target: str = ""
    label: str = ""
    distance: int = -1
    steps: List[str] = Field(default_factory=list)
    blocked_by_door: str = ""
    prerequisite: Optional[RequirementRow] = None
    unlocks_door: str = ""


RequirementRow.model_rebuild()


class RequirementResponse(BaseModel):
    campaign_id: str = ""
    action: str = ""
    target: str = ""
    ok: bool = False
    code: str = ""
    message: str = ""
    requirements: List[RequirementRow] = Field(default_factory=list)
    unplannable: List[RequirementTarget] = Field(default_factory=list)
    has_plan: bool = False


def _plan_state(store: GameStateStore, world: WorldDefinition, campaign_id: str) -> GameState:
    """The state a plan is computed against: the campaign, or a fresh sandbox."""
    if campaign_id:
        state = store.get_state(campaign_id)
        if state is None:
            raise HTTPException(
                status_code=404, detail=f"unknown campaign '{campaign_id}'"
            )
        return state.model_copy(deep=True)
    return world_service.build_initial_state(world, "__preview__")


def _persist_edit(store: GameStateStore, world: WorldDefinition) -> WorldDefinition:
    try:
        world_service.save_world(store, world)
    except world_service.WorldValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors) from exc
    return world


def _edit_world(world_id: str, edit):
    """Load, apply one structured edit, validate and save."""
    store = _get_store()
    try:
        world = _world_or_404(store, world_id)
        try:
            updated = edit(world)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except world_service.WorldValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.errors) from exc
        return _persist_edit(store, updated)
    finally:
        store.close()


class LocalModelStatus(BaseModel):
    configured: bool
    base_url: str
    model: str
    api_key_set: bool
    reachable: bool
    available_models: List[str] = Field(default_factory=list)
    detail: str = ""


def _get_store() -> GameStateStore:
    db_path = os.environ.get(
        "GAME_STATE_DB_PATH", os.path.join(_BACKEND_DIR, "game_state.db")
    )
    return GameStateStore(db_path)


def _get_scenario_store() -> ScenarioStore:
    db_path = os.environ.get(
        "SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db")
    )
    return ScenarioStore(db_path)


router_head = APIRouter()


@router_head.get("/world-presets", response_model=List[StarterWorldPresetView])
def list_world_presets() -> List[StarterWorldPresetView]:
    """The authored starter worlds a scenario's `world_type` can select.

    Read-only: this describes what a campaign *would* start in. Binding still
    happens through the normal campaign flow, so nothing here creates state.
    """
    return [_preset_summary(key) for key in starter_worlds.preset_keys()]


@router_head.get("/action-types", response_model=List[ActionSpecView])
def list_action_types():
    """The verbs the rules engine can decide, as declared in the catalogue.

    Editors and the debug console read this instead of hard-coding a list, so a
    new action shows up everywhere the moment it is registered.
    """
    return [spec.as_dict() for spec in all_actions()]

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
@router_head.post("/worlds", response_model=WorldWriteResponse)
def create_world(world: WorldDefinition):
    store = _get_store()
    try:
        summary = world_service.save_world(store, world)
    except world_service.WorldValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors) from exc
    finally:
        store.close()
    return {"status": "saved", **summary}


@router_head.get("/worlds", response_model=List[WorldSummary])
def list_worlds(scenario_id: Optional[str] = Query(None)):
    store = _get_store()
    try:
        worlds = store.list_worlds()
    finally:
        store.close()
    if scenario_id:
        worlds = [world for world in worlds if world.get("scenario_id") == scenario_id]
    return worlds


@router_head.post("/worlds/validate", response_model=ValidateResponse)
def validate_world_definition(world: WorldDefinition):
    errors = world_service.validate_world(world)
    return ValidateResponse(valid=not errors, errors=errors)


@router_head.post("/worlds/generate", response_model=WorldWriteResponse)
async def generate_world(body: GenerateWorldRequest):
    """Draft and save a new validated world from a prompt."""
    from uuid import uuid4

    from app.services import ai_generator

    scenario_store = _get_scenario_store()
    game_store = _get_store()
    try:
        if body.scenario_id and scenario_store.get_scenario(body.scenario_id) is None:
            raise HTTPException(status_code=404, detail="unknown scenario")
        preset = (
            scenario_store.get_model_preset(body.preset_id)
            if body.preset_id
            else None
        )
        if body.preset_id and preset is None:
            raise HTTPException(
                status_code=400, detail=f"unknown model preset '{body.preset_id}'"
            )
        router = ai_generator.router_for(preset)
        if router is None:
            raise HTTPException(
                status_code=503,
                detail="未配置可用模型：请先设置供应商密钥或本地模型",
            )
        world_id = f"ai_{uuid4().hex[:8]}"
        try:
            draft = await ai_generator.generate_world(
                router, body.prompt, body.scenario_id, world_id, body.language
            )
        except ai_generator.GenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        world = draft["world"]
        ai_generator.save_generated_world(game_store, world)
        return {
            "status": "saved",
            "world_id": world.world_id,
            "name": world.name,
            "counts": draft["counting"],
            "world": world.model_dump(mode="json"),
        }
    finally:
        game_store.close()
        scenario_store.close()


@router_head.post("/scenarios/author", response_model=AuthorScenarioResponse)
async def author_scenario(body: AuthorScenarioRequest):
    """Draft a whole scenario (world + knowledge + storyline) from one prompt.

    Everything the model returns is validated again here before it is stored.
    A partially valid draft is kept and its rejects are reported as warnings.
    """
    from uuid import uuid4

    from app.services import ai_generator, scenario_author

    scenario_store = _get_scenario_store()
    game_store = _get_store()
    try:
        preset = (
            scenario_store.get_model_preset(body.preset_id) if body.preset_id else None
        )
        if body.preset_id and preset is None:
            raise HTTPException(
                status_code=400, detail=f"unknown model preset '{body.preset_id}'"
            )

        scenario_id = body.scenario_id.strip()
        title = body.title.strip() or body.prompt.strip()[:40]
        if scenario_id:
            if scenario_store.get_scenario(scenario_id) is None:
                raise HTTPException(status_code=404, detail="unknown scenario")
        elif body.save:
            scenario = scenario_store.create_scenario(
                title=title, language="zh" if body.language == "中文" else "en"
            )
            scenario_id = scenario.id
        else:
            scenario_id = "draft"

        router_obj = ai_generator.router_for(preset)
        if router_obj is None:
            raise HTTPException(
                status_code=503,
                detail="未配置可用模型：请先设置供应商密钥或本地模型",
            )
        world_id = f"ai_{uuid4().hex[:8]}"
        try:
            draft = await scenario_author.author_scenario(
                router_obj,
                body.prompt,
                world_id,
                scenario_id,
                body.language,
                body.location_count,
            )
        except ai_generator.GenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc))

        world = draft.get("world")
        counts = {
            "locations": len(world.locations) if world else 0,
            "items": len(world.items) if world else 0,
            "entities": len(world.entities) if world else 0,
            "events": len(world.events) if world else 0,
            "knowledge": len(draft.get("knowledge") or []),
            "story_nodes": len(draft["storyline"].nodes) if draft.get("storyline") else 0,
        }
        if not body.save:
            return AuthorScenarioResponse(
                status="draft",
                scenario_id=body.scenario_id,
                scenario_title=title,
                warnings=list(draft.get("warnings") or []),
                counts=counts,
            )
        applied = scenario_author.apply_authored_scenario(
            scenario_store,
            game_store,
            draft,
            scenario_id,
            title,
            body.language,
        )
        return AuthorScenarioResponse(
            status="saved",
            scenario_id=applied["scenario_id"],
            scenario_title=applied["scenario_title"],
            world_id=applied["world_id"],
            world_saved=applied["world_saved"],
            storyline_saved=applied["storyline_saved"],
            knowledge_ids=applied["knowledge_ids"],
            warnings=applied["warnings"],
            counts=counts,
        )
    finally:
        game_store.close()
        scenario_store.close()


@router_head.post("/worlds/{world_id}/evolution/propose", response_model=EvolutionResponse)
def propose_world_evolution(world_id: str, body: EvolutionProposeRequest):
    """Draft what a turn would change. commit defaults to False."""
    game_store = _get_store()
    try:
        world = _world_or_404_detail(world_id)
        service = _evolution_service(game_store, _world_writer(game_store), body.auto_commit)
        report = service.run(
            world,
            _evolution_statements(body),
            campaign_id=body.campaign_id,
            observer=body.observer,
            source=body.source,
            commit=body.commit,
        )
        return _evolution_response(report)
    finally:
        game_store.close()


@router_head.get(
    "/worlds/{world_id}/evolution/proposals",
    response_model=List[EvolutionProposalView],
)
def list_world_evolution_proposals(
    world_id: str,
    status: str = "",
    kind: str = "",
    limit: int = 100,
):
    """The audit trail: what was proposed, what happened to it, and why."""
    store = _get_store()
    try:
        rows = store.list_proposals(
            world_id=world_id, status=status, kind=kind, limit=limit
        )
    finally:
        store.close()
    return [EvolutionProposalView(**_proposal_view(row)) for row in rows]


@router_head.post(
    "/worlds/{world_id}/evolution/proposals/{proposal_id}/approve",
    response_model=EvolutionResponse,
)
def approve_world_evolution_proposal(world_id: str, proposal_id: str):
    """Commit one pending proposal after a human decision."""
    game_store = _get_store()
    try:
        row = game_store.get_proposal(proposal_id)
        if row is None or row.get("world_id") != world_id:
            raise HTTPException(status_code=404, detail="unknown proposal")
        if row.get("status") != "pending":
            raise HTTPException(
                status_code=409, detail="proposal is {0}".format(row.get("status"))
            )
        world = _world_or_404_detail(world_id)
        service = _evolution_service(game_store, _world_writer(game_store), None)
        report = _commit_reviewed(
            game_store, _world_writer(game_store), world, row
        )
        return _evolution_response(report)
    finally:
        game_store.close()


@router_head.post(
    "/worlds/{world_id}/evolution/proposals/{proposal_id}/reject",
    response_model=EvolutionProposalView,
)
def reject_world_evolution_proposal(
    world_id: str, proposal_id: str, reason: str = ""
):
    """Refuse a proposal and keep the reason next to it."""
    store = _get_store()
    try:
        row = store.get_proposal(proposal_id)
        if row is None or row.get("world_id") != world_id:
            raise HTTPException(status_code=404, detail="unknown proposal")
        row["status"] = "rejected"
        row["error"] = reason or "rejected by reviewer"
        store.record_proposal(row)
        updated = store.get_proposal(proposal_id) or row
    finally:
        store.close()
    return EvolutionProposalView(**_proposal_view(updated))


@router_head.get("/worlds/{world_id}", response_model=WorldDefinition)
def get_world(world_id: str):
    store = _get_store()
    try:
        try:
            return world_service.get_world(store, world_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


@router_head.put("/worlds/{world_id}", response_model=WorldDefinition)
def replace_world(world_id: str, body: WorldDefinition):
    if body.world_id != world_id:
        raise HTTPException(
            status_code=400,
            detail=f"world_id '{body.world_id}' does not match the path id '{world_id}'",
        )
    store = _get_store()
    try:
        _world_or_404(store, world_id)
        from app.services import world_editor

        return _persist_edit(store, world_editor.replace_world(body))
    finally:
        store.close()


@router_head.delete("/worlds/{world_id}")
def delete_world(world_id: str):
    """Delete a world definition (campaign state rows are preserved)."""
    store = _get_store()
    try:
        _world_or_404(store, world_id)
        if not store.delete_world(world_id):
            raise HTTPException(status_code=404, detail=f"unknown world '{world_id}'")
        return {"deleted": world_id}
    finally:
        store.close()


@router_head.get("/worlds/{world_id}/map", response_model=WorldMapResponse)
def get_world_map(world_id: str, campaign_id: Optional[str] = None):
    """Navigation graph for one world (locations, exits, doors, occupants).

    This is a *projection* of the authoritative world document plus the
    static catalogues (items/entities/doors) resolved into names, so a client
    can draw a map without re-implementing reference resolution. Nothing here
    is authoritative run-time state: campaign state lives under /campaigns.

    Pass `campaign_id` to project *live* entity positions and health for that
    campaign instead of the authored starting layout; without it the map shows
    the world document exactly as authored.
    """
    world = _world_or_404_detail(world_id)
    items = {item.id: item for item in world.items}
    doors = {door.id: door for door in world.doors}
    entities = {entity.id: entity for entity in world.entities}
    live = _campaign_entities(world_id, campaign_id)

    def _present_at(location_id: str) -> List[str]:
        out: List[str] = []
        for entity in world.entities:
            if entity.kind == "object":
                continue
            record = live.get(entity.id)
            if record is not None:
                # Live position only draws once the campaign moved the entity;
                # otherwise the authored layout below stays authoritative.
                if record.location_id != location_id:
                    continue
                out.append(entity.id)
            elif entity.location_id == location_id:
                out.append(entity.id)
        return out

    def _item_ref(item_id: str) -> Dict[str, Any]:
        item = items.get(item_id)
        return {
            "id": item_id,
            "name": item.name if item else item_id,
            "known": item is not None,
        }

    def _door_ref(door_id: str) -> Optional[Dict[str, Any]]:
        door = doors.get(door_id)
        if door is None:
            return {"id": door_id, "name": door_id, "known": False}
        return {
            "id": door.id,
            "name": door.name,
            "location_id": door.location_id,
            "locked": door.locked,
            "key_item_id": door.key_item_id,
            "key_item": _item_ref(door.key_item_id) if door.key_item_id else None,
            "known": True,
        }

    locations: List[Dict[str, Any]] = []
    for location in world.locations:
        exits = []
        for exit_def in location.exits:
            exits.append(
                {
                    "target_id": exit_def.target_id,
                    "label": exit_def.label or "",
                    "door": _door_ref(exit_def.door_id) if exit_def.door_id else None,
                    "requires_item": (
                        _item_ref(exit_def.requires_item_id)
                        if exit_def.requires_item_id
                        else None
                    ),
                }
            )
        npcs = []
        present = _present_at(location.id)
        # Legacy location.npc_ids entries are display-only: keep them, unless the
        # entity is a live record that has since walked elsewhere.
        for entity_id in list(present) + [
            npc for npc in location.npc_ids if npc not in present
        ]:
            departed = live.get(entity_id)
            if departed is not None and departed.location_id != location.id:
                continue
            entity = entities.get(entity_id)
            npcs.append(
                {
                    "id": entity_id,
                    "name": entity.name if entity else entity_id,
                    "kind": entity.kind if entity else "npc",
                    "known": entity is not None,
                }
            )
        locations.append(
            {
                "id": location.id,
                "name": location.name,
                "description": location.description,
                "tags": list(location.tags),
                "region": location.region,
                "x": location.x,
                "y": location.y,
                "is_start": location.id == world.start_location_id,
                "exits": exits,
                "items": [_item_ref(item_id) for item_id in location.item_ids],
                "npcs": npcs,
                "doors": [
                    _door_ref(door_id) for door_id in location.door_ids if door_id
                ],
            }
        )

    regions: List[Dict[str, Any]] = []
    for location in world.locations:
        if not location.region:
            continue
        entry = next(
            (item for item in regions if item["id"] == location.region), None
        )
        if entry is None:
            entry = {"id": location.region, "locations": []}
            regions.append(entry)
        entry["locations"].append(location.id)

    return {
        "world_id": world.world_id,
        "scenario_id": world.scenario_id,
        "name": world.name,
        "description": world.description,
        "start_location_id": world.start_location_id,
        "regions": regions,
        "locations": locations,
        "items": [item.model_dump(mode="json") for item in world.items],
        "doors": [door.model_dump(mode="json") for door in world.doors],
        "entities": [
            {
                **entity.model_dump(mode="json"),
                **(
                    {
                        "location_id": live[entity.id].location_id,
                        "hp": live[entity.id].hp,
                        "max_hp": live[entity.id].max_hp,
                        "alive": live[entity.id].alive,
                        "inventory": dict(live[entity.id].inventory),
                    }
                    if entity.id in live
                    else {}
                ),
            }
            for entity in world.entities
        ],
        "counts": {
            "locations": len(world.locations),
            "items": len(world.items),
            "doors": len(world.doors),
            "entities": len(world.entities),
            "quests": len(world.quests),
            "events": len(world.events),
        },
    }


@router_head.get("/worlds/{world_id}/events", response_model=List[GameEventDef])
def list_world_events(world_id: str):
    store = _get_store()
    try:
        try:
            world = world_service.get_world(store, world_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [event.model_dump(mode="json") for event in world.events]
    finally:
        store.close()


# ------------------------------------------------------------------ campaign
