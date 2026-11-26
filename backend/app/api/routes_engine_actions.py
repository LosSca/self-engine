"""Running one turn: the action catalogue, combat, preview and narration.

Split out of `routes_engine` (see `app/api/routes_engine.py`), which is now the
facade that registers the groups in order. `router` below is this group's own
APIRouter; the handlers, their models and their guards did not change in the
move.
"""
from __future__ import annotations

import json
import logging

import asyncio
import os
import threading
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
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
    # "model" when the narrator wrote those options under the guidance pack,
    # "rules" when the deterministic builder supplied them (or both).
    suggestions_source: Optional[str] = None


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

    # How many recent turns the narrator may see is a panel setting: a long
    # scene wants more thread, a terse one fewer. Reads happen per turn, so a
    # change applies to the very next action.
    turns = HISTORY_TURNS
    try:
        from app.db.prefs_store import get_prefs_store

        store = get_prefs_store()
        try:
            raw = store.get("narrator_history_turns", None)
        finally:
            store.close()
        if raw is not None and raw != "":
            turns = max(1, min(int(float(raw)), 40))
    except (ImportError, TypeError, ValueError, OSError):
        turns = HISTORY_TURNS

    def provider(campaign_id: str, state) -> List[dict]:
        try:
            with _get_store() as store:
                bound = store.get_state(campaign_id) is not None
                # One committed request is projected as two records (the action
                # and the prose), so a request limit of HISTORY_TURNS keeps only
                # HISTORY_TURNS // 2 turns of thread. Ask for twice as many rows
                # to end up with the turn count the renderer will keep.
                events = (
                    store.narrative_history(campaign_id, limit=turns * 2)
                    if bound
                    else []
                )
            if not bound:
                db_path = os.environ.get("EVENT_DB_PATH", os.path.join(_BACKEND_DIR, "events.db"))
                legacy = EventStore(db_path)
                try:
                    events = legacy.get_recent(campaign_id, limit=turns * 2)
                finally:
                    legacy.close()
            note = render_history_note(events, limit=turns * 2)
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
            # The protagonist is not someone to address: "ask 旅人 what happened
            # here" on the first screen was the player asking themselves, and the
            # rules would answer "there is nobody here". Only the rest of the cast
            # is a conversational option.
            if getattr(character, "is_player", False):
                continue
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
    # When true the answer comes back as SSE: narrative deltas while the model
    # writes, then one final frame carrying the same payload the JSON lane
    # returns. Callers that do not ask for it keep the plain JSON contract.
    stream: bool = False


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


def _text_action_payload(campaign_id: str, result: Any) -> Any:
    """The turn envelope plus the three advisory next steps.

    Shared by the JSON and the streaming lane so both answer identically: the
    client shows three options after every turn, and the SSE lane sends them as
    a [SUGGESTIONS] tag while this one carries them in the response body.
    """
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


def _click_text(option: Mapping[str, Any]) -> str:
    """The utterance the button would send, mirroring the frontend's toInput."""
    speech = str(option.get("speech") or "").strip()
    action = str(option.get("action") or "").strip()
    target = str(option.get("target") or "").strip()
    text = ("[SAY] " + speech) if speech else ("[DO] " + action)
    if target and target not in text:
        text += " @" + target
    return text


def _executable_options(
    campaign_id: str, options: Sequence[Any]
) -> list[Dict[str, Any]]:
    """Drop options the rules engine would refuse if the player clicked them.

    The model is given the room's real names, but it can still phrase an option
    around something it merely saw in the prose ("检查控制台" when no console
    object exists). Clicking such a button produced a refused turn with an empty
    scene, so every option is re-read by the same deterministic parser the real
    turn uses and kept only when that parse resolves a real target.
    """
    if not options:
        return []
    try:
        from app.engines.action_registry import all_actions
        from app.services.text_action_service import PARSE_FAILURES, TextActionParser

        world = _load_world_for_campaign(campaign_id)
        state = _load_state_for_campaign(campaign_id)
        if world is None or state is None:
            return [dict(o) for o in options if isinstance(o, Mapping)]
        specs = {spec.name: spec for spec in all_actions()}
        parser = TextActionParser()
        kept: list[Dict[str, Any]] = []
        for option in options:
            if not isinstance(option, Mapping):
                continue
            parsed = parser.parse(_click_text(option), world, state, improvise=False)
            if parsed is None or parsed.confidence in PARSE_FAILURES:
                continue
            spec = specs.get(parsed.action)
            if spec is not None and getattr(spec, "target", "") == "required" and not parsed.target_id:
                continue
            kept.append(dict(option))
        return kept
    except Exception:  # noqa: BLE001 - verification is a filter, never a blocker
        logger.debug("option verification unavailable for %s", campaign_id, exc_info=True)
        return [dict(o) for o in options if isinstance(o, Mapping)]


def _story_hint(world: Any, state: Any) -> Dict[str, Any]:
    """The chapter the story is on and what the next one waits for.

    Options in storyline mode should be able to carry the plot forward, so the
    model is told the next chapter and the place it needs; without this the
    three buttons could circle one room forever and the ending stayed unreachable
    by clicking.
    """
    try:
        from app.engines.story_engine import storyline_status

        scenario_id = getattr(world, "scenario_id", "") or ""
        if not scenario_id or state is None:
            return {}
        with _get_scenario_store() as store:
            config = store.get_play_config(scenario_id)
        if config is None or config.mode != "storyline" or not config.storyline.nodes:
            return {}
        status = storyline_status(config.storyline, state, world)
        entered = set(status.entered)
        node_map = config.storyline.node_map()
        pending = [
            node
            for node in config.storyline.nodes
            if node.id not in entered and all(p in entered for p in node.prereq)
        ]
        if not pending:
            return {}
        next_node = pending[0]
        place_names = {loc.id: loc.name for loc in (getattr(world, "locations", None) or [])}
        goal = [
            place_names[condition.target]
            for condition in next_node.conditions
            if condition.kind == "at_location" and condition.target in place_names
        ]
        active = node_map.get(status.active or "")
        return {
            "chapter": getattr(active, "title", "") if active else "",
            "next": next_node.title,
            "goal": goal,
        }
    except Exception:  # noqa: BLE001 - a story hint is a nudge, never a blocker
        logger.debug("story hint unavailable", exc_info=True)
        return {}


def _load_state_for_campaign(campaign_id: str):
    """The committed GameState the option parser must read."""
    try:
        with GameStateStore(_game_state_db_path()) as game_store:
            return game_store.get_state(campaign_id)
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as exc:
        logger.debug("campaign state unavailable for %s: %s", campaign_id, exc)
        return None


async def _text_action_payload_async(campaign_id: str, result: Any) -> Any:
    """The turn envelope, with the next three options written by the model.

    The rule suggestions are built first and kept as the fallback, then the
    narrator model is asked for three options under a guidance pack (the room's
    real exits, doors, items and people plus the verbs the rules know). Every
    option it returns is checked against that pack, so a made-up target is
    dropped instead of becoming a button the engine would refuse.
    """
    payload = _text_action_payload(campaign_id, result)
    if not isinstance(payload, dict):
        return payload
    rules = payload.get("suggestions") or []
    try:
        from app.services.suggestions import allowed_targets, room_guidance
        from app.services.suggestions_model import merge_options, model_suggestions

        inner = payload.get("result")
        inner = inner if isinstance(inner, dict) else {}
        state = inner.get("state")
        if not isinstance(state, dict):
            dump = getattr(state, "model_dump", None)
            state = dump() if callable(dump) else {}
        world = inner.get("world") or _load_world_for_campaign(campaign_id)
        guidance = room_guidance(world, state, language=_campaign_language(campaign_id))
        narrative = inner.get("narrative")
        if isinstance(narrative, dict):
            guidance["recent"] = (narrative.get("text") or "")[-220:]
        story = _story_hint(world, _load_state_for_campaign(campaign_id))
        if story:
            guidance["story"] = story
        stats: dict[str, int] = {}
        chosen = _suggestion_settings()
        written = await model_suggestions(
            _turn_router(), guidance, fallback=(), stats=stats, **chosen
        )
        executable = _executable_options(campaign_id, written)
        if len(executable) < chosen["count"]:
            executable = merge_options(
                executable,
                rules,
                allowed_targets(guidance),
                limit=chosen["count"],
            )
        payload["suggestions"] = executable
        payload["suggestions_source"] = "model" if executable and written else "rules"
    except Exception:  # noqa: BLE001 - options must never break a turn
        logger.debug("model suggestions unavailable for %s", campaign_id, exc_info=True)
    return payload


#: Panel defaults for the option layer. The prefs store only holds what the
#: player changed, so every key keeps the value it had before it was editable.
_SUGGESTION_DEFAULTS: Dict[str, Any] = {
    "count": 3,
    "temperature": 0.8,
    "max_tokens": 400,
    "timeout": 12.0,
    "extra_rules": "",
    "allow_speech": True,
    "allow_investigate": True,
    "use_story": True,
}


def _suggestion_settings() -> Dict[str, Any]:
    """The option-layer knobs as stored in prefs, clamped to usable ranges.

    A wrong value in the table must never break a turn, so anything that does
    not parse falls back to the default and every number is clamped.
    """
    values = dict(_SUGGESTION_DEFAULTS)
    try:
        from app.db.prefs_store import get_prefs_store

        store = get_prefs_store()
        try:
            prefs = store.all() or {}
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - settings are advisory
        return values
    for pref_key, out_key, cast, low, high in (
        ("suggestions_count", "count", int, 2, 5),
        ("suggestions_temperature", "temperature", float, 0.0, 2.0),
        ("suggestions_max_tokens", "max_tokens", int, 60, 2000),
        ("suggestions_timeout", "timeout", float, 2.0, 120.0),
    ):
        raw = prefs.get(pref_key)
        if raw is None or raw == "":
            continue
        try:
            number = cast(float(raw))
        except (TypeError, ValueError):
            continue
        if cast is int:
            number = int(round(float(number)))
        # Out-of-range values are pulled to the nearest bound rather than
        # dropped: a slider that overshoots must not silently do nothing.
        values[out_key] = max(low, min(number, high))
    rules = prefs.get("suggestions_extra_rules")
    if isinstance(rules, str) and rules.strip():
        values["extra_rules"] = rules.strip()[:600]
    for pref_key, out_key in (
        ("suggestions_allow_speech", "allow_speech"),
        ("suggestions_allow_investigate", "allow_investigate"),
        ("suggestions_use_story", "use_story"),
    ):
        raw = prefs.get(pref_key)
        if isinstance(raw, bool):
            values[out_key] = raw
        elif isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
            values[out_key] = raw.strip().lower() == "true"
    return values


def _turn_router() -> Any:
    """The narrator router, so options come from the configured model."""
    try:
        from app.api.routes_game import _llm

        return _llm
    except Exception:  # noqa: BLE001 - no router means rule options
        return None


def _sse_json(frame: Dict[str, Any]) -> str:
    """One SSE event holding a JSON envelope (json.dumps keeps it single-line)."""
    return "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"


def _stream_text_action(campaign_id: str, request: TextActionBody) -> StreamingResponse:
    """Same turn, delivered while the narrator writes.

    Frames: {"type":"delta","text":...} as the model emits prose, then a single
    {"type":"result","payload":...} with the committed state, effects and the
    next three options, then {"type":"done"}. Anything that fails before the
    result arrives is reported as {"type":"error","message":...} so the client
    can tell a failed turn from an empty one.
    """
    async def event_stream():
        service = _service()
        text_service = TextActionService(
            service.store,
            scenario_store=service.scenario_store,
            action_service=service,
            advisor=_build_advisor(),
        )
        queue: "asyncio.Queue[str]" = asyncio.Queue()

        def on_delta(delta: str) -> None:
            if delta:
                queue.put_nowait(delta)

        task = asyncio.create_task(
            text_service.execute(
                campaign_id,
                request.text,
                request_id=request.request_id,
                narrate=request.narrate,
                on_delta=on_delta,
            )
        )
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    piece = getter.result()
                    if piece:
                        yield _sse_json({"type": "delta", "text": piece})
                    continue
                getter.cancel()
                break
            while not queue.empty():
                piece = queue.get_nowait()
                if piece:
                    yield _sse_json({"type": "delta", "text": piece})
            error = None if task.cancelled() else task.exception()
            if error is not None:
                if isinstance(error, LookupError):
                    yield _sse_json({"type": "error", "message": "无法继续：这个存档不存在或已被删除。", "code": "not_found"})
                else:
                    logger.exception("text-action stream failed for %s", campaign_id, exc_info=error)
                    yield _sse_json({"type": "error", "message": str(error) or error.__class__.__name__, "code": "failed"})
                yield _sse_json({"type": "done"})
                return
            payload = await _text_action_payload_async(campaign_id, task.result())
            yield _sse_json({"type": "result", "payload": payload})
            yield _sse_json({"type": "done"})
        finally:
            if not task.done():
                task.cancel()
            service.store.close()
            service.scenario_store.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/campaigns/{campaign_id}/text-actions", response_model=TextActionResponse)
async def run_text_action(campaign_id: str, request: TextActionBody):
    """Parse, decide and commit one utterance through the same rules lane.

    The response carries the parsed interpretation (action + target + how it was
    matched), so a wrong guess is visible instead of silently changing state.
    Pass `stream: true` to receive the narrative as it is written.
    """
    if request.stream:
        return _stream_text_action(campaign_id, request)
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
    return await _text_action_payload_async(campaign_id, result)
