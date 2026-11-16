"""Running one turn: the action catalogue, combat, preview and narration.

Split out of `routes_engine` (see `app/api/routes_engine.py`), which is now the
facade that registers the groups in order. `router` below is this group's own
APIRouter; the handlers, their models and their guards did not change in the
move.
"""
from __future__ import annotations

import logging

import asyncio
import os
import threading
from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.db.game_state_store import GameStateStore, StateConflictError
from app.db.scenario_store import ScenarioStore
from app.engines.action_registry import all_actions, get_action
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
from app.engines import npc_schedule
from app.services import authoring_session, interaction_planner, starter_worlds, world_service
from app.services import npc_turns
from app.api.routes_game import _game_state_db_path
from app.services.action_service import ActionService
from app.services import narrator_factory
from app.services.text_action_service import TextActionService
from app.services.suggestions import hints_from_state, suggestions_for_result


from app.api.routes_engine_state import (  # noqa: E402
    _BACKEND_DIR,
    _get_scenario_store,
    _get_store,
    _narrator_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter()



class ActionSpecView(BaseModel):
    """One entry of the action catalogue: what the rules can decide, and the
    words that map onto it. The catalogue is the source of truth, so a new
    action shows up here the moment it is registered."""

    name: str
    label: str = ""
    target: Literal["none", "optional", "required"] = "none"
    description: str = ""
    verbs: List[str] = Field(default_factory=list)


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


# Every call site goes through this name so the narrator factory has one
# patchable binding: `routes_engine._get_narrator`. Reading the facade at call
# time means a test that swaps it takes effect even though the handler lives
# here (see the re-exports at the bottom of app/api/routes_engine.py).
def current_narrator():
    """Resolve the narrator at call time, through the patched facade name."""
    import app.api.routes_engine as facade

    return facade._get_narrator()


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
                # One committed request is projected as two records (the action
                # and the prose), so a request limit of HISTORY_TURNS keeps only
                # HISTORY_TURNS // 2 turns of thread. Ask for twice as many rows
                # to end up with the turn count the renderer will keep.
                events = (
                    store.narrative_history(campaign_id, limit=HISTORY_TURNS * 2)
                    if bound
                    else []
                )
            if not bound:
                db_path = os.environ.get("EVENT_DB_PATH", os.path.join(_BACKEND_DIR, "events.db"))
                legacy = EventStore(db_path)
                try:
                    events = legacy.get_recent(campaign_id, limit=HISTORY_TURNS * 2)
                finally:
                    legacy.close()
            note = render_history_note(events, limit=HISTORY_TURNS * 2)
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
    """Whether this campaign's world clock is frozen.

    The rule is "a campaign someone is playing runs, and taking a turn is that
    playing". A campaign with no cached legacy session used to read as paused,
    which is right for a world nobody is in front of and wrong for the
    sessionless lane, where every turn would then happen at a standstill. The
    saved clock is the authority: freezing and resuming are explicit actions of
    their own, so nothing here has to infer them from who is watching.
    """
    try:
        from app.db.game_state_store import GameStateStore

        with GameStateStore(_game_state_db_path(), migrate=False) as store:
            state = store.get_state(campaign_id)
        if state is None:
            return False
        return bool(getattr(state, "clock_paused", False))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("could not read the clock for %s: %s", campaign_id, exc)
        return False


def _tick_handlers(service: Optional[ActionService] = None) -> Dict[str, Any]:
    """Handlers for scheduled events that come due inside a turn's clock tick.

    Routines are the only scheduled thing today: when an NPC's shift starts,
    the handler records the slot on the NPC (activity, and the slow drift of
    its needs) inside the same transaction that advanced the clock.
    """
    return {npc_schedule.NPC_ROUTINE: npc_turns.routine_handler(service)}


def _event_store():
    """The legacy event stream, opened per service.

    NPC minds are stored there as `NPC_THOUGHT` events (the panel in
    routes_game rebuilds them from the same rows), so the rules lane needs a
    handle of its own to write them.
    """
    from app.db.event_store import EventStore

    return EventStore(os.environ.get("EVENT_DB_PATH", os.path.join(_BACKEND_DIR, "events.db")))


def _service() -> ActionService:
    scenario_store = _get_scenario_store()
    service = ActionService(
        _get_store(),
        narrator=current_narrator(),
        context_provider=_make_context_provider(scenario_store),
        history_provider=_history_provider(),
        settled_sink=_graph_mirror_sink(),
        clock_paused=_clock_paused,
        tick_handlers={},
        # NPC minds live in the event stream; this lane never wrote there, so
        # the panel was showing an empty table with a working button.
        event_store=_event_store(),
    )
    service.tick_handlers = _tick_handlers(service)
    # The NPCs act through this very service: one action model, one rules
    # engine, one write path. Their routines are scheduled events, so they are
    # ticked by the same clock the player's actions advance.
    service.npc_round = npc_turns.run_round
    service.npc_clock = npc_turns.start_world_clock
    #: The town's plan for what should happen next. The domain only ever sees a
    #: callable that may propose scheduled events - it gets no write path, and
    #: every proposal it makes still has to come due and run through a handler.
    from app.services import town_advisor  # local: keeps the import graph acyclic

    service.tick_advice = lambda svc, state: town_advisor.advise(
        svc, state, llm=_get_town_llm()
    )
    #: Closed together with the game store by the request handler.
    service.scenario_store = scenario_store
    return service


_town_llm_cache: list = []


def _get_town_llm():
    """The model the town planner may ask, or None when none is configured.

    Same choice of provider as the narrator, because a world that can narrate
    in a language can also plan in it. Built once: the router keeps its own
    retry state and rebuilding it per request would throw that away.
    """
    from app.services import town_advisor

    if not town_advisor.enabled():
        return None
    if not _town_llm_cache:
        try:
            _town_llm_cache.append(narrator_factory.build_router())
        except Exception as exc:  # noqa: BLE001 - the planner is optional
            logger.warning("town planner has no model: %s", exc)
            _town_llm_cache.append(None)
    return _town_llm_cache[0]


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
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        logger.debug("campaign cast unavailable for %s: %s", campaign_id, exc)
    return people[:limit]


def _campaign_language(campaign_id: str) -> str:
    """Language the campaign was written in (suggestions are localized)."""
    try:
        with _get_scenario_store() as store:
            campaign = store.get_campaign(campaign_id)
            scenario = store.get_scenario(campaign.scenario_id) if campaign else None
        return getattr(scenario, "language", "") or "zh"
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        logger.debug("campaign language unavailable for %s: %s", campaign_id, exc)
        return "zh"


def _load_world_for_campaign(campaign_id: str):
    """The rules world a campaign is bound to (worlds live in the game store)."""
    try:
        with GameStateStore(_game_state_db_path()) as game_store:
            state = game_store.get_state(campaign_id)
            if state is None or not getattr(state, "world_id", ""):
                return None
            return game_store.get_world(state.world_id)
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        logger.debug("campaign world unavailable for %s: %s", campaign_id, exc)
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
            # The committed state decides which doors are still shut and where
            # people and items actually are; the world document alone would
            # describe the room as it was authored.
            doors=getattr(state, "doors", None),
            item_locations=getattr(state, "item_locations", None),
            entities=getattr(state, "entities", None),
            # A running fight, the scene trail and the quest progress decide
            # whether the screen leads with a fight, repeats a question or
            # walks back the way the player came.
            combat=getattr(state, "combat", None),
            flags=getattr(state, "flags", None),
            story_nodes=getattr(state, "story_nodes", None),
        )
    except Exception:  # noqa: BLE001 - hints must never break a screen
        logger.debug("hints unavailable for %s", campaign_id, exc_info=True)
        return []


class CombatActionBody(BaseModel):
    """A grid-free combat command; the rules engine decides the outcome."""

    action: Literal["attack", "defend", "flee"] = "attack"
    target_id: Optional[str] = Field(None, max_length=120)
    request_id: Optional[str] = Field(None, max_length=128)
    narrate: bool = True


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


class TextActionBody(BaseModel):
    """A free-text utterance resolved into the structured action lane."""

    text: str = Field(..., min_length=1, max_length=2000)
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


@router.get("/campaigns/{campaign_id}/suggestions")
def campaign_suggestions(campaign_id: str) -> Dict[str, Any]:
    """What the player could do next, for the state the campaign is in now."""
    return {"suggestions": _current_hints(campaign_id)}


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
