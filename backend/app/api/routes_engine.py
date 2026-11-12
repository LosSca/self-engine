"""Versioned engine API (/api/v1).

The existing /api/game surface stays untouched for compatibility; this router is
the structured, rule-driven path: worlds are configuration, state is
authoritative, every action carries a request_id and every result reports the
state version it produced.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import settings
from app.db.game_state_store import GameStateStore
from app.db.scenario_store import ScenarioStore
from app.engines.action_registry import all_actions, get_action
from app.engines.event_engine import diff_states, simulate_event, validate_event
from app.engines.rules_engine import RulesEngine
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
from app.api.routes_game import _game_state_db_path
from app.services.action_service import ActionService
from app.services import state_diagnostics
from app.services.knowledge_service import build_context
from app.engines.agent_router import AGENT_ROLES, load_agent_configs
from app.engines.story_engine import storyline_status
from app.models.knowledge import ScenarioPlayContext
from app.services.narrative_service import LLMNarrator, TemplateNarrator
from app.services import narrator_factory
from app.services.text_action_service import TextActionService
from app.services.suggestions import hints_from_state, suggestions_for_result

logger = logging.getLogger(__name__)

router = APIRouter(tags=["engine"])


class ActionSpecView(BaseModel):
    """One entry of the action catalogue: what the rules can decide, and the
    words that map onto it. The catalogue is the source of truth, so a new
    action shows up here the moment it is registered."""

    name: str
    label: str = ""
    target: Literal["none", "optional", "required"] = "none"
    description: str = ""
    verbs: List[str] = Field(default_factory=list)


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


class SnapshotView(BaseModel):
    """One retained state snapshot; stored_bytes is cumulative, oldest first."""

    version: int
    request_id: str = ""
    created_at: str = ""
    bytes: int = 0
    stored_bytes: int = 0


class TextActionResponse(BaseModel):
    """A free-text utterance resolved into the structured lane.

    `parsed` is the interpreter's own account of the match (action, target and
    how it was found); `result` is the committed ActionResult. A refusal still
    carries both, so a wrong guess is visible without changing state.
    """

    request_id: str
    status: str
    # An utterance that could not be parsed has no interpretation; the refusal
    # still carries the raw text in `result` so the player sees what happened.
    parsed: Optional[Dict[str, Any]] = None
    result: ActionResult
    # Three advisory next steps (same shape as the SSE [SUGGESTIONS] tag).
    # Optional: a campaign without a world still returns a turn.
    suggestions: List[Dict[str, Any]] = Field(default_factory=list)





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


@router.get("/world-presets", response_model=List[StarterWorldPresetView])
def list_world_presets() -> List[StarterWorldPresetView]:
    """The authored starter worlds a scenario's `world_type` can select.

    Read-only: this describes what a campaign *would* start in. Binding still
    happens through the normal campaign flow, so nothing here creates state.
    """
    return [_preset_summary(key) for key in starter_worlds.preset_keys()]


@router.get("/action-types", response_model=List[ActionSpecView])
def list_action_types():
    """The verbs the rules engine can decide, as declared in the catalogue.

    Editors and the debug console read this instead of hard-coding a list, so a
    new action shows up everywhere the moment it is registered.
    """
    return [spec.as_dict() for spec in all_actions()]

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_narrator_cache: Dict[str, Any] = {}


def _get_store() -> GameStateStore:
    db_path = os.environ.get(
        "GAME_STATE_DB_PATH", os.path.join(_BACKEND_DIR, "game_state.db")
    )
    return GameStateStore(db_path)


def _get_narrator():
    """Narrator for the engine lane, from the shared factory.

    `ENGINE_NARRATOR` is `auto` by default: the model is used when one is
    configured (a local NInfer server counts), the template otherwise. The
    decision lives in app.services.narrator_factory so both lanes agree.
    """
    key = narrator_factory.describe()["choice"] + ":" + narrator_factory.describe()["provider"]
    if key not in _narrator_cache:
        narrator, _provider = narrator_factory.build_narrator("ENGINE_NARRATOR")
        _narrator_cache[key] = narrator
    return _narrator_cache[key]


def _get_scenario_store() -> ScenarioStore:
    db_path = os.environ.get(
        "SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db")
    )
    return ScenarioStore(db_path)


def _make_context_provider(store: ScenarioStore, campaign_id: str = ""):
    """Resolve advisory scenario context, including its authored voice and language."""
    from app.services.narrative_service import scenario_play_context

    def provider(scenario_id: str, campaign_id: str = ""):
        return scenario_play_context(store, scenario_id, campaign_id)

    return provider


def _history_provider():
    """Recall what this campaign already committed, for the narrator only.

    This is a read of the append-only event log, nothing more: the recap never
    feeds back into the rules, and any failure degrades to no recap at all
    rather than failing the turn.
    """
    from app.db.event_store import EventStore
    from app.services.narrative_service import HISTORY_TURNS, render_history_note

    def provider(campaign_id: str, state) -> List[dict]:
        try:
            with _get_store() as store:
                bound = store.get_state(campaign_id) is not None
                events = store.narrative_history(campaign_id, limit=HISTORY_TURNS) if bound else []
            if not bound:
                db_path = os.environ.get("EVENT_DB_PATH", os.path.join(_BACKEND_DIR, "events.db"))
                legacy = EventStore(db_path)
                try:
                    events = legacy.get_recent(campaign_id, limit=HISTORY_TURNS * 2)
                finally:
                    legacy.close()
            note = render_history_note(events, limit=HISTORY_TURNS)
        except Exception as exc:  # noqa: BLE001 - recap is optional
            logger.warning("history recap unavailable for %s: %s", campaign_id, exc)
            return []
        if not note:
            return []
        return [
            {
                "id": f"history:{campaign_id}",
                "layer": "history",
                "title": "recent turns",
                "content": note,
            }
        ]

    return provider


def _graph_mirror_enabled() -> bool:
    """Mirror committed turns into the knowledge graph unless told not to.

    Read from the environment on every turn so a test (or an operator) can
    turn it off without a restart; ENGINE_GRAPH_MIRROR=0 disables it.
    """
    raw = (os.environ.get("ENGINE_GRAPH_MIRROR") or "1").strip().lower()
    return raw not in ("0", "off", "false", "no", "none", "")


def _neo4j_settings():
    uri = (os.environ.get("NEO4J_URI") or "").strip() or "bolt://localhost:7687"
    user = (os.environ.get("NEO4J_USER") or "").strip() or "neo4j"
    password = (os.environ.get("NEO4J_PASSWORD") or "").strip()
    return uri, user, password


def _graph_mirror_sink():
    """Build the post-commit mirror: committed turns become graph episodes.

    Returns None when no password is configured, so a machine without Neo4j
    behaves exactly as before (nothing is attempted). The mirror only reads
    what was committed; it can never change a turn.
    """
    from app.engines.graphiti_engine import GraphitiEngine

    uri, user, password = _neo4j_settings()
    if not password:
        return None
    try:
        engine = GraphitiEngine(uri, user, password)
    except Exception as exc:  # noqa: BLE001 - graphiti is an optional extra
        logger.info("knowledge graph mirror unavailable: %s", exc)
        return None

    def sink(campaign_id: str, state, narrative) -> None:
        if not _graph_mirror_enabled():
            return
        text = getattr(narrative, "text", "") or ""
        if not text.strip():
            return

        def _run() -> None:
            try:
                asyncio.run(
                    engine.ingest_episode(
                        campaign_id=campaign_id,
                        text=text,
                        description="narrator_response",
                    )
                )
            except Exception:  # noqa: BLE001 - a mirror must never hurt a turn
                logger.warning(
                    "knowledge graph mirror failed for %s", campaign_id, exc_info=True
                )

        threading.Thread(
            target=_run, name=f"graph-mirror-{campaign_id}", daemon=True
        ).start()

    return sink


def _clock_paused(campaign_id: str) -> bool:
    """Whether this campaign's world clock is frozen (the player left)."""
    try:
        from app.api import routes_game

        return bool(routes_game.campaign_clock_paused(campaign_id))
    except Exception:  # noqa: BLE001 - a missing runtime means "not paused"
        return False


def _service() -> ActionService:
    scenario_store = _get_scenario_store()
    service = ActionService(
        _get_store(),
        narrator=_get_narrator(),
        context_provider=_make_context_provider(scenario_store),
        history_provider=_history_provider(),
        settled_sink=_graph_mirror_sink(),
        clock_paused=_clock_paused,
    )
    #: Closed together with the game store by the request handler.
    service.scenario_store = scenario_store
    return service


def _build_advisor():
    """Wire the optional intent advisor from the same router the narrator uses.

    Returns None when the switch is off or no model is reachable, so the text
    lane behaves exactly as before by default.
    """
    from app.services import intent_advisor

    if not intent_advisor.enabled():
        return None
    try:
        router = narrator_factory.build_router()
    except Exception as exc:  # noqa: BLE001 - advice is optional
        logger.info("intent advisor could not build a router: %s", exc)
        return None
    if router is None:
        return None
    return intent_advisor.IntentAdvisor(router)


class BindWorldRequest(BaseModel):
    world_id: str = Field(..., min_length=1)
    reset: bool = False


class ValidateResponse(BaseModel):
    valid: bool
    errors: List[str] = Field(default_factory=list)


class RollbackRequest(BaseModel):
    version: int = Field(..., ge=0)
    reason: str = ""


class RepairRequest(BaseModel):
    dry_run: bool = True


class ImportCampaignRequest(BaseModel):
    state: Dict[str, Any]
    events: List[Dict[str, Any]] = Field(default_factory=list)
    reset: bool = False


class GenerateWorldRequest(BaseModel):
    scenario_id: str = ""
    prompt: str = Field(..., min_length=1, max_length=4000)
    preset_id: str = ""
    language: str = "中文"


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


class LaneResponse(BaseModel):
    """GET /campaigns/{id}/lane: which lane answers this campaign."""

    campaign_id: str
    lane: Literal["engine", "narrative"]
    bound: bool
    world_id: str = ""
    version: int = 0


class CampaignExportResponse(BaseModel):
    format: str
    format_version: int
    exported_at: str
    state: GameState
    events: List[Dict[str, Any]] = Field(default_factory=list)


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


class ActionPreviewResponse(BaseModel):
    """A dry run: the same rules decided, nothing was written."""

    campaign_id: str
    state_version: int
    action: str
    label: str = ""
    target_id: Optional[str] = None
    ok: bool
    code: str = ""
    message: str = ""
    effects: List[Dict[str, Any]] = Field(default_factory=list)
    changed: bool = False


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
@router.post("/worlds", response_model=WorldWriteResponse)
def create_world(world: WorldDefinition):
    store = _get_store()
    try:
        summary = world_service.save_world(store, world)
    except world_service.WorldValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors) from exc
    finally:
        store.close()
    return {"status": "saved", **summary}


@router.get("/worlds", response_model=List[WorldSummary])
def list_worlds(scenario_id: Optional[str] = Query(None)):
    store = _get_store()
    try:
        worlds = store.list_worlds()
    finally:
        store.close()
    if scenario_id:
        worlds = [world for world in worlds if world.get("scenario_id") == scenario_id]
    return worlds


@router.post("/worlds/validate", response_model=ValidateResponse)
def validate_world_definition(world: WorldDefinition):
    errors = world_service.validate_world(world)
    return ValidateResponse(valid=not errors, errors=errors)


@router.post("/worlds/generate", response_model=WorldWriteResponse)
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


@router.post("/scenarios/author", response_model=AuthorScenarioResponse)
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

@router.post("/worlds/{world_id}/evolution/propose", response_model=EvolutionResponse)
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


@router.get(
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

@router.post(
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


@router.post(
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

@router.get("/worlds/{world_id}", response_model=WorldDefinition)
def get_world(world_id: str):
    store = _get_store()
    try:
        try:
            return world_service.get_world(store, world_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        store.close()


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


@router.get("/worlds/{world_id}/map", response_model=WorldMapResponse)
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


@router.get("/worlds/{world_id}/events", response_model=List[GameEventDef])
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
@router.post("/campaigns/{campaign_id}/world", response_model=GameState)
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


@router.get("/campaigns/{campaign_id}/lane", response_model=LaneResponse)
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


@router.get("/campaigns/{campaign_id}/export", response_model=CampaignExportResponse)
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


@router.post("/campaigns/{campaign_id}/import", response_model=GameState)
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


@router.get("/campaigns/{campaign_id}/state", response_model=GameState)
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


@router.get("/campaigns/{campaign_id}/story", response_model=CampaignStoryResponse)
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


@router.get("/campaigns/{campaign_id}/snapshots/prune", response_model=SnapshotPruneResponse)
def campaign_snapshots_prune(campaign_id: str):
    """Drop old snapshots by hand (same policy the commit path applies)."""
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
        game_store.commit_rollback(campaign_id=campaign_id, state=restored, events=[event])
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
        game_store.commit_rollback(
            campaign_id=campaign_id, state=repaired, events=[event]
        )
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


class CombatActionBody(BaseModel):
    """A grid-free combat command; the rules engine decides the outcome."""

    action: Literal["attack", "defend", "flee"] = "attack"
    target_id: Optional[str] = Field(None, max_length=120)
    request_id: Optional[str] = Field(None, max_length=128)
    narrate: bool = True


@router.post("/campaigns/{campaign_id}/combat", response_model=ActionResult)
async def run_combat_action(campaign_id: str, request: CombatActionBody):
    """Expose attack/defend/flee to the UI without inventing a second lane.

    Everything goes through ActionService, so an LLM never decides who wins;
    the combat round is recorded in the same transaction as the state change.
    """
    service = _service()
    try:
        return await service.execute(
            ActionRequest(
                campaign_id=campaign_id,
                request_id=request.request_id or "",
                action=request.action,
                target_id=request.target_id,
                narrate=request.narrate,
            )
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        service.store.close()
        service.scenario_store.close()


class ActionPreviewBody(BaseModel):
    """A dry run: the same rules decide, nothing is written.

    Preview exists so an author can ask "would this work?" without spending a
    request id or touching the campaign. It deliberately carries no client
    request id - a dry run is never recorded, so it cannot collide with a real
    turn.
    """

    campaign_id: Optional[str] = Field(None, max_length=64)
    action: str = Field(..., min_length=1, max_length=64)
    actor_id: str = "player"
    target_id: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/campaigns/{campaign_id}/actions/preview", response_model=ActionPreviewResponse
)
def preview_action(campaign_id: str, request: ActionPreviewBody):
    """Decide one action against the committed state without committing it.

    The answer is exactly what the rules would say right now (allowed or the
    refusal code and message), plus the world's current version so a caller can
    tell whether the state it previewed is still current.
    """
    if request.campaign_id and request.campaign_id != campaign_id:
        raise HTTPException(
            status_code=400,
            detail="campaign_id in the path and in the body must match",
        )
    store = _get_store()
    try:
        state = store.get_state(campaign_id)
        if state is None:
            raise HTTPException(
                status_code=404, detail=f"campaign '{campaign_id}' has no bound world"
            )
        world = world_service.get_world(store, state.world_id)
        try:
            spec = get_action(request.action)
        except Exception:  # noqa: BLE001 - an unknown verb is a client error
            spec = None
        outcome = RulesEngine().evaluate(
            world, state, request.action, request.target_id, request.params
        )
        return {
            "campaign_id": campaign_id,
            "state_version": state.version,
            "action": request.action,
            "label": spec.label if spec is not None else "",
            "target_id": request.target_id,
            "ok": outcome.ok,
            "code": outcome.code,
            "message": outcome.message,
            "effects": [effect.model_dump(mode="json") for effect in outcome.effects],
            "changed": outcome.ok and bool(outcome.effects),
        }
    finally:
        store.close()


@router.get("/actions", response_model=List[ActionSpecView])
def list_actions():
    """The catalogue the rules engine can decide, with the words it parses.

    Authors read this instead of guessing verbs: an action exists when it is
    registered in app/engines/action_registry.py, never because a world says so.
    """
    return [spec.as_dict() for spec in all_actions()]


@router.post("/campaigns/{campaign_id}/actions", response_model=ActionResult)
async def run_action(campaign_id: str, request: ActionRequest):
    if request.campaign_id != campaign_id:
        raise HTTPException(
            status_code=400,
            detail="campaign_id in the path and in the body must match",
        )
    service = _service()
    try:
        return await service.execute(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        service.store.close()
        service.scenario_store.close()



def _campaign_characters(campaign_id: str, limit: int = 8) -> list[tuple[str, str]]:
    """(name, entity id) for characters the player could address right now."""
    people: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        name = value if isinstance(value, str) else getattr(value, "name", None)
        text = (name or "").strip() if isinstance(name, str) else ""
        if not text or text.casefold() in seen:
            return
        seen.add(text.casefold())
        entity_id = getattr(value, "entity_id", None) or getattr(value, "id", None) or ""
        people.append((text, entity_id if isinstance(entity_id, str) else ""))

    try:
        with _get_scenario_store() as store:
            campaign = store.get_campaign(campaign_id)
            cast = store.list_characters(campaign.scenario_id) if campaign else []
        for character in cast:
            add(character)
    except Exception:
        logger.debug("campaign cast unavailable for %s", campaign_id, exc_info=True)
    return people[:limit]


def _campaign_language(campaign_id: str) -> str:
    """Language the campaign was written in (suggestions are localized)."""
    try:
        with _get_scenario_store() as store:
            campaign = store.get_campaign(campaign_id)
            scenario = store.get_scenario(campaign.scenario_id) if campaign else None
        return getattr(scenario, "language", "") or "zh"
    except Exception:
        return "zh"


def _load_world_for_campaign(campaign_id: str):
    """The rules world a campaign is bound to (worlds live in the game store)."""
    try:
        with GameStateStore(_game_state_db_path()) as game_store:
            state = game_store.get_state(campaign_id)
            if state is None or not getattr(state, "world_id", ""):
                return None
            return game_store.get_world(state.world_id)
    except Exception:
        logger.debug("campaign world unavailable for %s", campaign_id, exc_info=True)
        return None

def _scenario_bound_world(campaign_id: str, game_store: GameStateStore):
    """A world already authored for this campaign's scenario, if any."""
    try:
        with _get_scenario_store() as store:
            campaign = store.get_campaign(campaign_id)
        if campaign is None:
            return None
        for summary in game_store.list_worlds():
            if summary.get("scenario_id") != campaign.scenario_id:
                continue
            return game_store.get_world(summary["world_id"])
    except Exception:  # noqa: BLE001 - hints are advisory
        logger.debug("scenario world unavailable for %s", campaign_id, exc_info=True)
    return None


def _current_hints(campaign_id: str) -> list[Dict[str, Any]]:
    """Three options for the campaign's present state (no turn required).

    suggestions_for_result needs a finished ActionResult, so the very first
    screen of a campaign had no choices at all: the player had to guess at
    an empty input box. This reads the state the engine already committed
    and asks the same builder.
    """
    try:
        with GameStateStore(_game_state_db_path()) as game_store:
            state = game_store.get_state(campaign_id)
            world = None
            if state is None:
                # Nothing committed yet (the first screen of a campaign). The
                # rules lane would bind a world on the first turn; here we only
                # look, so no world is created just to draw three buttons.
                location_id, inventory = "", []
                world = _scenario_bound_world(campaign_id, game_store)
            else:
                location_id = getattr(state, "location_id", "") or ""
                inventory = list((getattr(state, "inventory", None) or {}).keys())
                world_id = getattr(state, "world_id", "") or ""
                world = game_store.get_world(world_id) if world_id else None
        return hints_from_state(
            location_id=location_id,
            inventory=inventory,
            characters=_campaign_characters(campaign_id),
            world=world,
            language=_campaign_language(campaign_id),
        )
    except Exception:  # noqa: BLE001 - hints must never break a screen
        logger.debug("hints unavailable for %s", campaign_id, exc_info=True)
        return []


@router.get("/campaigns/{campaign_id}/suggestions")
def campaign_suggestions(campaign_id: str) -> Dict[str, Any]:
    """What the player could do next, for the state the campaign is in now."""
    return {"suggestions": _current_hints(campaign_id)}


class TextActionBody(BaseModel):
    """A free-text utterance resolved into the structured action lane."""

    text: str = Field(..., min_length=1, max_length=2000)
    request_id: Optional[str] = Field(None, max_length=128)
    narrate: bool = True


@router.post("/campaigns/{campaign_id}/text-actions", response_model=TextActionResponse)
async def run_text_action(campaign_id: str, request: TextActionBody):
    """Parse, decide and commit one utterance through the same rules lane.

    The response carries the parsed interpretation (action + target + how it was
    matched), so a wrong guess is visible instead of silently changing state.
    """
    service = _service()
    text_service = TextActionService(
        service.store,
        scenario_store=service.scenario_store,
        action_service=service,
        advisor=_build_advisor(),
    )
    try:
        result = await text_service.execute(
            campaign_id,
            request.text,
            request_id=request.request_id,
            narrate=request.narrate,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        service.store.close()
        service.scenario_store.close()
    # The client shows three options after every turn; the SSE lane emits them
    # as a [SUGGESTIONS] tag, this lane returns them in the response body.
    try:
        dump = getattr(result, "model_dump", None)
        payload = result if isinstance(result, dict) else (dump() if callable(dump) else {})
        payload = dict(payload)
        inner = payload.get("result")
        inner = dict(inner) if isinstance(inner, dict) else {}
        payload["suggestions"] = suggestions_for_result(
            inner,
            characters=_campaign_characters(campaign_id),
            world_getter=lambda: _load_world_for_campaign(campaign_id),
            language=_campaign_language(campaign_id),
        )
        return payload
    except Exception:  # noqa: BLE001 - suggestions must never break a turn
        logger.debug("suggestions unavailable for %s", campaign_id, exc_info=True)
        return result


@router.get("/campaigns/{campaign_id}/events", response_model=List[DomainEventView])
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


@router.get(
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


class ProgressObjectiveBody(BaseModel):
    """An author or debugger asserting that one objective advanced."""

    amount: int = Field(1, ge=1, le=100)
    request_id: str = Field(
        "", max_length=128, description="replays of the same id are not applied twice"
    )


@router.post(
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


@router.get(
    "/campaigns/{campaign_id}/event-executions",
    response_model=List[EventExecutionView],
)
def list_event_executions(
    campaign_id: str,
    limit: int = Query(100, ge=1, le=500),
    status: Optional[str] = Query(None),
    after_version: int = Query(0, ge=0),
):
    """Event execution history, newest first.

    Derived from the committed `event.fired`/`event.failed`/`event.skipped`
    domain events, so it can only ever show what actually happened.
    """
    wanted = (status or "").strip().lower()
    store = _get_store()
    try:
        rows = store.list_domain_events(
            campaign_id,
            event_type_prefix="event.",
            limit=500 if wanted else limit,
            after_version=after_version,
        )
    finally:
        store.close()

    executions: List[Dict[str, Any]] = []
    for row in rows:
        outcome = row["event_type"].split(".", 1)[1] if "." in row["event_type"] else ""
        if wanted and outcome != wanted:
            continue
        payload = row.get("payload") or {}
        executions.append(
            {
                "event_id": payload.get("event_id", ""),
                "name": payload.get("name", ""),
                "status": outcome,
                "reason": payload.get("reason", ""),
                "effects": payload.get("effects", []),
                "branch": payload.get("branch", ""),
                "parent_event_id": payload.get("parent_event_id", ""),
                "depth": payload.get("depth", 0),
                "state_version": row["state_version"],
                "request_id": row["request_id"],
                "created_at": row["created_at"],
            }
        )
        if len(executions) >= limit:
            break
    return executions


@router.get("/campaigns/{campaign_id}/requests", response_model=List[RequestSummaryView])
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


@router.get("/agents", response_model=AgentRolesResponse)
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


@router.get("/campaigns/{campaign_id}/agent-calls", response_model=List[AgentCallView])
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


@router.get("/actions/{request_id}", response_model=RequestRecordResponse)
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


def _authoring_world(store, world_id: str):
    try:
        return _world_or_404(store, world_id)
    finally:
        store.close()


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


@router.post("/worlds/{world_id}/authoring/propose", response_model=AuthoringProposalResponse)
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


@router.post("/worlds/{world_id}/authoring/commit", response_model=AuthoringCommitResponse)
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


@router.post("/worlds/{world_id}/plan", response_model=RoomPlanResponse)
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


@router.post("/worlds/{world_id}/preview-actions", response_model=PreviewChainResponse)
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


@router.post("/worlds/{world_id}/explain", response_model=RequirementResponse)
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
@router.post(
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


@router.post("/worlds/{world_id}/events/{event_id}/simulate")
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
def _world_or_404(store: GameStateStore, world_id: str) -> WorldDefinition:
    try:
        return world_service.get_world(store, world_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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


@router.put("/worlds/{world_id}", response_model=WorldDefinition)
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


@router.delete("/worlds/{world_id}")
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


@router.put("/worlds/{world_id}/locations/{location_id}", response_model=WorldDefinition)
def upsert_location(world_id: str, location_id: str, body: LocationDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_location(world, body, location_id))


@router.delete("/worlds/{world_id}/locations/{location_id}", response_model=WorldDefinition)
def delete_location(world_id: str, location_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_location(world, location_id))


@router.put("/worlds/{world_id}/items/{item_id}", response_model=WorldDefinition)
def upsert_item(world_id: str, item_id: str, body: ItemDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_item(world, body, item_id))


@router.delete("/worlds/{world_id}/items/{item_id}", response_model=WorldDefinition)
def delete_item(world_id: str, item_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_item(world, item_id))


@router.put("/worlds/{world_id}/doors/{door_id}", response_model=WorldDefinition)
def upsert_door(world_id: str, door_id: str, body: DoorDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_door(world, body, door_id))


@router.delete("/worlds/{world_id}/doors/{door_id}", response_model=WorldDefinition)
def delete_door(world_id: str, door_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_door(world, door_id))


@router.put("/worlds/{world_id}/entities/{entity_id}", response_model=WorldDefinition)
def upsert_entity(world_id: str, entity_id: str, body: EntityDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_entity(world, body, entity_id))


@router.delete("/worlds/{world_id}/entities/{entity_id}", response_model=WorldDefinition)
def delete_entity(world_id: str, entity_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_entity(world, entity_id))


@router.put("/worlds/{world_id}/events/{event_id}", response_model=WorldDefinition)
def upsert_event(world_id: str, event_id: str, body: GameEventDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_event(world, body, event_id))


@router.delete("/worlds/{world_id}/events/{event_id}", response_model=WorldDefinition)
def delete_event(world_id: str, event_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_event(world, event_id))


@router.put("/worlds/{world_id}/quests/{quest_id}", response_model=WorldDefinition)
def upsert_quest(world_id: str, quest_id: str, body: QuestDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_quest(world, body, quest_id))


@router.delete("/worlds/{world_id}/quests/{quest_id}", response_model=WorldDefinition)
def delete_quest(world_id: str, quest_id: str):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.delete_quest(world, quest_id))


@router.get("/worlds/{world_id}/quests", response_model=List[QuestDef])
def list_world_quests(world_id: str):
    """Read-only quest definitions of one world, in authored order."""
    game_store = _get_store()
    try:
        return _world_or_404(game_store, world_id).quests
    finally:
        game_store.close()


@router.post(
    "/worlds/{world_id}/quests", response_model=WorldDefinition, status_code=201
)
def create_quest(world_id: str, body: QuestDef):
    from app.services import world_editor

    return _edit_world(world_id, lambda world: world_editor.upsert_quest(world, body))


@router.get("/worlds/{world_id}/quests/{quest_id}", response_model=QuestDef)
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


@router.patch(
    "/worlds/{world_id}/quests/{quest_id}", response_model=WorldDefinition
)
def patch_quest(world_id: str, quest_id: str, body: QuestDef):
    """Replace one quest; the path id is authoritative, never the body's."""
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.upsert_quest(world, body, quest_id)
    )


@router.put(
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


@router.delete(
    "/worlds/{world_id}/quests/{quest_id}/objectives/{objective_id}",
    response_model=WorldDefinition,
)
def delete_quest_objective(world_id: str, quest_id: str, objective_id: str):
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.delete_objective(world, quest_id, objective_id)
    )


@router.put(
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


@router.delete(
    "/worlds/{world_id}/quests/{quest_id}/rewards/{reward_id}",
    response_model=WorldDefinition,
)
def delete_quest_reward(world_id: str, quest_id: str, reward_id: str):
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.delete_reward(world, quest_id, reward_id)
    )


@router.put(
    "/worlds/{world_id}/custom-actions/{action_id}", response_model=WorldDefinition
)
def upsert_custom_action(world_id: str, action_id: str, body: CustomActionDef):
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.upsert_custom_action(world, body, action_id)
    )


@router.delete(
    "/worlds/{world_id}/custom-actions/{action_id}", response_model=WorldDefinition
)
def delete_custom_action(world_id: str, action_id: str):
    from app.services import world_editor

    return _edit_world(
        world_id, lambda world: world_editor.delete_custom_action(world, action_id)
    )


# ---------------------------------------------------------------- local model
class LocalModelStatus(BaseModel):
    configured: bool
    base_url: str
    model: str
    api_key_set: bool
    reachable: bool
    available_models: List[str] = Field(default_factory=list)
    detail: str = ""


@router.get("/local-model", response_model=LocalModelStatus)
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