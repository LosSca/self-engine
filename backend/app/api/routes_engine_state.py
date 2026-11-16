"""Campaign state: the bound world, snapshots, rollback/repair and read-only progress.

Split out of `routes_engine` (see `app/api/routes_engine.py`), which is now the
facade that registers the groups in order. `router` below is this group's own
APIRouter; the handlers, their models and their guards did not change in the
move.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.config import settings
from app.db.game_state_store import GameStateStore, StateConflictError
from app.db.scenario_store import ScenarioStore
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
from app.services import state_diagnostics
from app.engines.story_engine import storyline_status
from app.services import narrator_factory


_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class CampaignExportResponse(BaseModel):
    format: str
    format_version: int
    exported_at: str
    state: GameState
    events: List[Dict[str, Any]] = Field(default_factory=list)


class ImportCampaignRequest(BaseModel):
    state: Dict[str, Any]
    events: List[Dict[str, Any]] = Field(default_factory=list)
    reset: bool = False


_narrator_cache: Dict[str, Any] = {}


router_head = APIRouter()
router = APIRouter()



class SnapshotView(BaseModel):
    """One retained state snapshot; stored_bytes is cumulative, oldest first."""

    version: int
    request_id: str = ""
    created_at: str = ""
    bytes: int = 0
    stored_bytes: int = 0


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


class RollbackRequest(BaseModel):
    version: int = Field(..., ge=0)
    reason: str = ""


class RepairRequest(BaseModel):
    dry_run: bool = True


def _state_conflict(exc: StateConflictError) -> HTTPException:
    """Report a lost compare-and-set as 409 instead of letting it become a 500.

    Both debug write paths (/rollback, /repair) read the state, mutate it in
    memory, then write it back with the version they started from. When a turn
    commits in between, the write is refused — and the caller must be told the
    state moved on, not handed an internal error.
    """
    return HTTPException(status_code=409, detail=str(exc))


def _debug_tools_enabled() -> bool:
    """Whether state-mutating debug endpoints (rollback, repair) may run.

    `ENGINE_DEBUG_TOOLS` is authoritative when set. When it is not set, the
    answer is derived from config.settings.debug so that a production install
    (no DEBUG) does not expose rollback to anyone who can reach the port. Set
    it to 1 explicitly to keep the tools while running the engine locally.
    """
    flag = os.environ.get("ENGINE_DEBUG_TOOLS", "").strip().lower()
    if flag:
        return flag not in ("0", "false", "off", "no")
    return bool(getattr(settings, "debug", False))


def _require_debug_tools() -> None:
    if not _debug_tools_enabled():
        raise HTTPException(
            status_code=403,
            detail="debug tools are disabled (set ENGINE_DEBUG_TOOLS=1 in development)",
        )


# ------------------------------------------------------- response contracts
# Every route that returns a hand-built dict now says *which* dict. The models
# below are the wire contract: FastAPI validates the payload on the way out, so
# a renamed key or a dropped field fails loudly here instead of silently
# changing what the browser receives. They are intentionally permissive about
# nested engine payloads (effects, issues, diffs) while pinning the top level.


class LaneResponse(BaseModel):
    """GET /campaigns/{id}/lane: which lane answers this campaign."""

    campaign_id: str
    lane: Literal["engine", "narrative"]
    bound: bool
    world_id: str = ""
    version: int = 0


class StoryNodeView(BaseModel):
    id: str
    title: str = ""
    kind: str = "main"
    line: str = ""
    summary: str = ""
    requires_event_pack: Optional[str] = None
    locked: bool = False


class StoryPayload(BaseModel):
    name: str = ""
    nodes: List[StoryNodeView] = Field(default_factory=list)
    lines: List[Dict[str, Any]] = Field(default_factory=list)
    packs: List[Dict[str, Any]] = Field(default_factory=list)


class CampaignStoryResponse(BaseModel):
    """GET /campaigns/{id}/story: storyline progress, read-only."""

    campaign_id: str
    mode: Literal["free", "storyline"]
    entered: List[str] = Field(default_factory=list)
    active: Optional[str] = None
    pending: List[str] = Field(default_factory=list)
    locked: List[str] = Field(default_factory=list)
    reasons: Dict[str, str] = Field(default_factory=dict)
    armed_packs: List[str] = Field(default_factory=list)
    fired_packs: List[str] = Field(default_factory=list)
    storyline: StoryPayload


class NarrationHealth(BaseModel):
    status: Literal["ok", "degraded", "unreported"]
    window: int
    recent_failures: int
    last_failure: Optional[Dict[str, Any]] = None
    narrator: Dict[str, Any] = Field(default_factory=dict)


class CampaignDiagnosticsResponse(BaseModel):
    campaign_id: str
    world_id: str
    version: int
    ok: bool
    errors: int
    warnings: int
    issues: List[Dict[str, Any]] = Field(default_factory=list)
    narration: NarrationHealth


class SnapshotPruneResponse(BaseModel):
    campaign_id: str
    dropped: int
    remaining: int
    bytes: int


class StateDiffResponse(BaseModel):
    campaign_id: str
    from_version: int
    to_version: int
    changed: Dict[str, Any] = Field(default_factory=dict)


class RollbackResponse(BaseModel):
    status: str
    restored_version: int
    state: GameState


class RepairResponse(BaseModel):
    status: str
    applied: List[str] = Field(default_factory=list)
    remaining: List[Dict[str, Any]] = Field(default_factory=list)
    issues: List[Dict[str, Any]] = Field(default_factory=list)
    state: GameState


class QuestObjectiveView(BaseModel):
    id: str
    kind: str
    target: str = ""
    description: str = ""
    required_count: int = 1
    count: int = 0
    done: bool = False


class QuestProgressView(BaseModel):
    quest_id: str
    title: str = ""
    description: str = ""
    enabled: bool = True
    status: str = "inactive"
    objectives: List[QuestObjectiveView] = Field(default_factory=list)
    completed_objectives: int = 0
    total_objectives: int = 0
    rewards: List[Dict[str, Any]] = Field(default_factory=list)


class QuestProgressResponse(BaseModel):
    campaign_id: str
    world_id: str
    version: int
    quests: List[QuestProgressView] = Field(default_factory=list)
    active: List[str] = Field(default_factory=list)
    completed: List[str] = Field(default_factory=list)
    failed: List[str] = Field(default_factory=list)


class EventExecutionView(BaseModel):
    event_id: str = ""
    name: str = ""
    status: str = ""
    reason: str = ""
    effects: List[Any] = Field(default_factory=list)
    branch: str = ""
    parent_event_id: str = ""
    depth: int = 0
    state_version: int = 0
    request_id: str = ""
    created_at: str = ""


def _campaign_world_and_config(game_store, scenario_store, campaign_id):
    state = game_store.get_state(campaign_id)
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"campaign '{campaign_id}' has no bound world"
        )
    world = _world_or_404(game_store, state.world_id)
    config = None
    scenario_id = getattr(world, "scenario_id", "") or ""
    if scenario_id and scenario_store is not None and scenario_store.get_scenario(scenario_id):
        config = scenario_store.get_play_config(scenario_id)
    return state, world, config


def _narration_health(game_store, campaign_id: str, limit: int = 50) -> Dict[str, Any]:
    """Summarize how narration has been going, without reading any model output.

    A narrator that fails every turn is otherwise invisible: the turn still
    commits, which is correct, but nothing reports that prose is missing. This
    counts the recorded failures so a stuck model shows up in /diagnostics.
    """
    events = game_store.list_domain_events(
        campaign_id, event_type_prefix="narrative.", limit=limit
    )
    failures = [event for event in events if event["event_type"] == "narrative.failed"]
    if events:
        status = "degraded" if failures else "ok"
    else:
        status = "unreported"
    try:
        narrator = narrator_factory.describe()
    except Exception:  # pragma: no cover - never let a panel break diagnostics
        narrator = {"choice": "unknown"}
    return {
        "status": status,
        "window": limit,
        "recent_failures": len(failures),
        "last_failure": (failures[0] if failures else None),
        "narrator": narrator,
    }


def _world_or_404(store: GameStateStore, world_id: str) -> WorldDefinition:
    try:
        return world_service.get_world(store, world_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router_head.get("/campaigns/{campaign_id}/lane", response_model=LaneResponse)
def get_campaign_lane(campaign_id: str):
    """Which engine answers this campaign: 'engine' (rules) or 'narrative' (LLM).

    A campaign becomes rule-driven the moment a world is bound to it. Until
    then it keeps the legacy free-form lane, so existing saves are untouched.
    """
    store = _get_store()
    try:
        state = store.get_state(campaign_id)
    finally:
        store.close()
    if state is None:
        return {
            "campaign_id": campaign_id,
            "lane": "narrative",
            "bound": False,
            "world_id": "",
            "version": 0,
        }
    return {
        "campaign_id": campaign_id,
        "lane": "engine",
        "bound": True,
        "world_id": state.world_id,
        "version": state.version,
    }


@router_head.get("/campaigns/{campaign_id}/export", response_model=CampaignExportResponse)
def export_campaign(campaign_id: str):
    """Download the authoritative snapshot and its domain events."""
    store = _get_store()
    try:
        state = store.get_state(campaign_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"campaign '{campaign_id}' is not bound")
        return {
            "format": "lunar-campaign",
            "format_version": 1,
            "exported_at": datetime.utcnow().isoformat(),
            "state": state.model_dump(mode="json"),
            "events": store.list_events(campaign_id, limit=100000),
        }
    finally:
        store.close()


@router_head.post("/campaigns/{campaign_id}/import", response_model=GameState)
def import_campaign(campaign_id: str, body: ImportCampaignRequest):
    """Restore a previously exported snapshot into a campaign.

    Import is destructive: an existing campaign needs `reset=true`. The world
    must still exist, and the campaign id is always taken from the URL, so a
    file cannot overwrite a different campaign by naming it inside the payload.
    """
    store = _get_store()
    try:
        existing = store.get_state(campaign_id)
        if existing is not None and not body.reset:
            raise HTTPException(
                status_code=409,
                detail="campaign already exists; pass reset=true to overwrite it",
            )
        raw = dict(body.state or {})
        raw["campaign_id"] = campaign_id
        try:
            state = GameState.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - authored input
            raise HTTPException(status_code=400, detail=f"invalid campaign state: {exc}")
        if state.version < 0:
            raise HTTPException(status_code=400, detail="state version cannot be negative")
        if store.get_world(state.world_id) is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown world '{state.world_id}'",
            )
        store.bind_campaign(
            state,
            clear_history=body.reset,
            events=body.events,
        )
        return state
    finally:
        store.close()


@router_head.get("/campaigns/{campaign_id}/state", response_model=GameState)
def get_campaign_state(campaign_id: str):
    store = _get_store()
    try:
        state = store.get_state(campaign_id)
    finally:
        store.close()
    if state is None:
        raise HTTPException(
            status_code=404, detail=f"campaign '{campaign_id}' has no bound world"
        )
    return state


@router_head.get("/campaigns/{campaign_id}/story", response_model=CampaignStoryResponse)
def get_campaign_story(campaign_id: str):
    """Story progress for one campaign (read-only; storyline mode only)."""
    game_store = _get_store()
    scenario_store = _get_scenario_store()
    try:
        state = game_store.get_state(campaign_id)
        if state is None:
            raise HTTPException(
                status_code=404, detail=f"campaign '{campaign_id}' has no bound world"
            )
        try:
            world = world_service.get_world(game_store, state.world_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        scenario_id = getattr(world, "scenario_id", "") or ""
        config = (
            scenario_store.get_play_config(scenario_id)
            if scenario_id and scenario_store.get_scenario(scenario_id)
            else None
        )
        if config is None or config.mode != "storyline":
            return {
                "campaign_id": campaign_id,
                "mode": "free",
                "entered": list(state.story_nodes),
                "active": None,
                "pending": [],
                "locked": [],
                "reasons": {},
                "armed_packs": [],
                "fired_packs": [],
                "storyline": {"name": "", "nodes": [], "lines": [], "packs": []},
            }
        status = storyline_status(config.storyline, state, world)
        return {
            "campaign_id": campaign_id,
            "mode": "storyline",
            "entered": status.entered,
            "active": status.active,
            "pending": status.pending,
            "locked": status.locked,
            "reasons": status.reasons,
            "armed_packs": status.armed_packs,
            "fired_packs": status.fired_packs,
            "storyline": {
                "name": config.storyline.name,
                "nodes": [
                    {
                        "id": node.id,
                        "title": node.title,
                        "kind": node.kind,
                        "line": node.line,
                        "summary": node.summary,
                        "requires_event_pack": node.requires_event_pack,
                        "locked": node.locked,
                    }
                    for node in config.storyline.nodes
                ],
                "lines": [line.model_dump(mode="json") for line in config.storyline.lines],
                "packs": [pack.model_dump(mode="json") for pack in config.storyline.packs],
            },
        }
    finally:
        game_store.close()
        scenario_store.close()
@router.get("/campaigns/{campaign_id}/diagnostics", response_model=CampaignDiagnosticsResponse)
def campaign_diagnostics(campaign_id: str):
    game_store = _get_store()
    scenario_store = _get_scenario_store()
    try:
        state, world, config = _campaign_world_and_config(
            game_store, scenario_store, campaign_id
        )
        mode = config.mode if config else "free"
        storyline = config.storyline if config else None
        issues = state_diagnostics.check_state(world, state, storyline, mode)
        return {
            "campaign_id": campaign_id,
            "world_id": state.world_id,
            "version": state.version,
            "ok": not state_diagnostics.has_blocking_issues(issues),
            "errors": sum(1 for issue in issues if issue.severity == "error"),
            "warnings": sum(1 for issue in issues if issue.severity == "warning"),
            "issues": [issue.as_dict() for issue in issues],
            "narration": _narration_health(game_store, campaign_id),
        }
    finally:
        game_store.close()
        scenario_store.close()


@router.get("/campaigns/{campaign_id}/snapshots", response_model=List[SnapshotView])
def campaign_snapshots(campaign_id: str, limit: int = Query(100, ge=1, le=500)):
    store = _get_store()
    try:
        return store.list_snapshots(campaign_id, limit=limit)
    finally:
        store.close()


@router.delete("/campaigns/{campaign_id}/snapshots/prune", response_model=SnapshotPruneResponse)
def campaign_snapshots_prune(campaign_id: str):
    """Drop old snapshots by hand (same policy the commit path applies).

    DELETE, not GET: this destroys rows, and a GET is fired by link prefetching,
    crawlers and browser history. Guarded like rollback/repair — it used to be
    the only state-mutating endpoint without _require_debug_tools().
    """
    _require_debug_tools()
    store = _get_store()
    try:
        dropped = store.prune_snapshots(campaign_id)
        remaining = len(store.list_snapshots(campaign_id, limit=500))
        stats = store.snapshot_stats(campaign_id)
    finally:
        store.close()
    return {
        "campaign_id": campaign_id,
        "dropped": dropped,
        "remaining": remaining,
        "bytes": stats["total_bytes"],
    }


@router.get("/campaigns/{campaign_id}/snapshots/{version}", response_model=GameState)
def campaign_snapshot(campaign_id: str, version: int):
    store = _get_store()
    try:
        snapshot = store.get_snapshot(campaign_id, version)
    finally:
        store.close()
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"no snapshot for campaign '{campaign_id}' at version {version}",
        )
    return snapshot


@router.get("/campaigns/{campaign_id}/state-diff", response_model=StateDiffResponse)
def campaign_state_diff(
    campaign_id: str,
    from_version: int = Query(..., ge=0),
    to_version: int = Query(..., ge=0),
):
    store = _get_store()
    try:
        before = store.get_snapshot(campaign_id, from_version)
        after = store.get_snapshot(campaign_id, to_version)
    finally:
        store.close()
    missing = [
        version
        for version, snapshot in ((from_version, before), (to_version, after))
        if snapshot is None
    ]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"missing snapshot(s) for version(s) {missing}",
        )
    return {
        "campaign_id": campaign_id,
        "from_version": from_version,
        "to_version": to_version,
        "changed": diff_states(before, after),
    }


@router.post("/campaigns/{campaign_id}/rollback", response_model=RollbackResponse)
def campaign_rollback(campaign_id: str, body: RollbackRequest):
    _require_debug_tools()
    game_store = _get_store()
    try:
        current = game_store.get_state(campaign_id)
        if current is None:
            raise HTTPException(
                status_code=404, detail=f"campaign '{campaign_id}' has no bound world"
            )
        if body.version == current.version:
            raise HTTPException(
                status_code=400,
                detail=f"campaign is already at version {current.version}",
            )
        snapshot = game_store.get_snapshot(campaign_id, body.version)
        if snapshot is None:
            raise HTTPException(
                status_code=404,
                detail=f"no snapshot for campaign '{campaign_id}' at version {body.version}",
            )
        restored = snapshot.model_copy(deep=True)
        restored.campaign_id = campaign_id
        restored.version = current.version + 1
        restored.updated_at = datetime.utcnow().isoformat()
        event = {
            "event_type": "state.rolled_back",
            "payload": {
                "from_version": current.version,
                "restored_version": body.version,
                "new_version": restored.version,
                "reason": body.reason,
            },
            "state_version": restored.version,
        }
        # Pass the version we based this rollback on: a concurrent turn that lands
        # between get_state() above and this write must not be silently overwritten.
        try:
            game_store.commit_rollback(
                campaign_id=campaign_id,
                state=restored,
                events=[event],
                expected_version=current.version,
            )
        except StateConflictError as exc:
            raise _state_conflict(exc) from exc
        return {
            "status": "rolled_back",
            "restored_version": body.version,
            "state": restored.model_dump(mode="json"),
        }
    finally:
        game_store.close()


@router.post("/campaigns/{campaign_id}/repair", response_model=RepairResponse)
def campaign_repair(campaign_id: str, body: RepairRequest):
    _require_debug_tools()
    game_store = _get_store()
    scenario_store = _get_scenario_store()
    try:
        state, world, config = _campaign_world_and_config(
            game_store, scenario_store, campaign_id
        )
        mode = config.mode if config else "free"
        storyline = config.storyline if config else None
        issues = state_diagnostics.check_state(world, state, storyline, mode)
        result = state_diagnostics.repair_state(
            world, state, issues=issues, storyline=storyline
        )
        applied = result.applied
        if body.dry_run or not applied:
            return {
                "status": "dry_run" if body.dry_run else "no_changes",
                "applied": applied,
                "issues": [issue.as_dict() for issue in issues],
                "state": result.state.model_dump(mode="json"),
            }
        repaired = result.state
        repaired.version = state.version + 1
        repaired.updated_at = datetime.utcnow().isoformat()
        event = {
            "event_type": "state.repaired",
            "payload": {"from_version": state.version, "applied": applied},
            "state_version": repaired.version,
        }
        # Same compare-and-set as rollback: repair reads the state, mutates it in
        # memory and writes it back, so a turn that commits meanwhile must make
        # this fail loudly instead of being overwritten.
        try:
            game_store.commit_rollback(
                campaign_id=campaign_id,
                state=repaired,
                events=[event],
                expected_version=state.version,
            )
        except StateConflictError as exc:
            raise _state_conflict(exc) from exc
        remaining = state_diagnostics.check_state(world, repaired, storyline, mode)
        return {
            "status": "repaired",
            "applied": applied,
            "remaining": [issue.as_dict() for issue in remaining],
            "state": repaired.model_dump(mode="json"),
        }
    finally:
        game_store.close()
        scenario_store.close()



