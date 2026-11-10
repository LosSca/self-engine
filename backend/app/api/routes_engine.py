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
from app.engines.action_registry import all_actions
from app.engines.event_engine import diff_states, simulate_event, validate_event
from app.models.game import (
    ActionResult,
    ActionRequest,
    CustomActionDef,
    DoorDef,
    EntityDef,
    GameEventDef,
    GameState,
    ItemDef,
    LocationDef,
    QuestDef,
    WorldDefinition,
)
from app.services import world_service
from app.services.action_service import ActionService
from app.services import state_diagnostics
from app.services.knowledge_service import build_context
from app.engines.story_engine import storyline_status
from app.models.knowledge import ScenarioPlayContext
from app.services.narrative_service import LLMNarrator, TemplateNarrator
from app.services import narrator_factory
from app.services.text_action_service import TextActionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["engine"])


@router.get("/action-types", response_model=List[Dict[str, Any]])
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


def _service() -> ActionService:
    scenario_store = _get_scenario_store()
    service = ActionService(
        _get_store(),
        narrator=_get_narrator(),
        context_provider=_make_context_provider(scenario_store),
        history_provider=_history_provider(),
        settled_sink=_graph_mirror_sink(),
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


# --------------------------------------------------------------------- worlds
@router.post("/worlds", response_model=Dict[str, Any])
def create_world(world: WorldDefinition):
    store = _get_store()
    try:
        summary = world_service.save_world(store, world)
    except world_service.WorldValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors) from exc
    finally:
        store.close()
    return {"status": "saved", **summary}


@router.get("/worlds", response_model=List[Dict[str, Any]])
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


@router.post("/worlds/generate", response_model=Dict[str, Any])
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


@router.get("/worlds/{world_id}/map", response_model=Dict[str, Any])
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


@router.get("/worlds/{world_id}/events", response_model=List[Dict[str, Any]])
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


@router.get("/campaigns/{campaign_id}/lane", response_model=Dict[str, Any])
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


@router.get("/campaigns/{campaign_id}/export", response_model=Dict[str, Any])
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


@router.get("/campaigns/{campaign_id}/story", response_model=Dict[str, Any])
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


@router.get("/campaigns/{campaign_id}/diagnostics", response_model=Dict[str, Any])
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


@router.get("/campaigns/{campaign_id}/snapshots", response_model=List[Dict[str, Any]])
def campaign_snapshots(campaign_id: str, limit: int = Query(100, ge=1, le=500)):
    store = _get_store()
    try:
        return store.list_snapshots(campaign_id, limit=limit)
    finally:
        store.close()


@router.get("/campaigns/{campaign_id}/snapshots/prune", response_model=Dict[str, Any])
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


@router.get("/campaigns/{campaign_id}/state-diff", response_model=Dict[str, Any])
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


@router.post("/campaigns/{campaign_id}/rollback", response_model=Dict[str, Any])
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


@router.post("/campaigns/{campaign_id}/repair", response_model=Dict[str, Any])
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


class TextActionBody(BaseModel):
    """A free-text utterance resolved into the structured action lane."""

    text: str = Field(..., min_length=1, max_length=2000)
    request_id: Optional[str] = Field(None, max_length=128)
    narrate: bool = True


@router.post("/campaigns/{campaign_id}/text-actions", response_model=Dict[str, Any])
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
        return await text_service.execute(
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


@router.get("/campaigns/{campaign_id}/events", response_model=List[Dict[str, Any]])
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
    "/campaigns/{campaign_id}/quest-progress", response_model=Dict[str, Any]
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


@router.get(
    "/campaigns/{campaign_id}/event-executions",
    response_model=List[Dict[str, Any]],
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


@router.get("/actions/{request_id}", response_model=Dict[str, Any])
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