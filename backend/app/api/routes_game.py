import json
import logging
import os
from dataclasses import asdict
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings

from app.services.game_session import GameSession
from app.engines.agent_router import AgentRuntime, load_agent_configs
from app.engines.narrator_engine import NarratorEngine
from app.engines.memory_engine import MemoryEngine, CrystalTier
from app.engines.world_reactor import WorldReactor
from app.engines.journal_engine import JournalEngine, JournalCategory
from app.engines.combat_engine import CombatEngine
from app.engines.plot_generator import PlotGenerator
from app.engines.npc_mind_engine import NpcMindEngine
from app.engines.inventory_engine import InventoryEngine
from app.engines.llm_router import LLMRouter, LLMConfig, LLMProvider, reset_call_log, get_call_summary, get_call_trace
from app.engines.opening_generator import (
    format_setup_lines,
    generate_opening,
)
from app.db.event_store import EventStore, EventType
from app.db.game_state_store import GameStateStore
from app.db.scenario_store import ScenarioStore
from app.db.trace_store import TraceStore
from app.services.action_service import ActionService
from app.services.narrative_service import LLMNarrator, TemplateNarrator
from app.services.text_action_service import TextActionService
from app.services.scenario_interpolation import interpolate

logger = logging.getLogger(__name__)

router = APIRouter()

# Ensure API keys from config are available in os.environ for litellm
if not os.environ.get("ANTHROPIC_API_KEY") and settings.anthropic_api_key:
    os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
if not os.environ.get("DEEPSEEK_API_KEY") and settings.deepseek_api_key:
    os.environ["DEEPSEEK_API_KEY"] = settings.deepseek_api_key
if not os.environ.get("OPENAI_API_KEY") and settings.openai_api_key:
    os.environ["OPENAI_API_KEY"] = settings.openai_api_key

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_event_store = EventStore(os.environ.get("EVENT_DB_PATH", os.path.join(_BACKEND_DIR, "events.db")))
_trace_store = TraceStore(os.environ.get("LLM_TRACE_DB_PATH", os.path.join(_BACKEND_DIR, "traces.db")))
_llm = LLMRouter(LLMConfig())

# Secondary calls (audit, memory, journal, combat, NPCs, plot, opening) run on a
# cheaper model than the narrative call.
_OPENAI_MODEL = "gpt-5.6-sol"
_AUXILIARY_MODELS = {
    LLMProvider.ANTHROPIC: "claude-sonnet-5",
    LLMProvider.DEEPSEEK: "deepseek-v4-flash",
    LLMProvider.OPENAI: _OPENAI_MODEL,
    # The local NInfer server exposes a single model; auxiliary calls reuse it.
    LLMProvider.LOCAL: settings.local_llm_model,
}


def apply_model_policy(provider: LLMProvider, model: str) -> None:
    """Narrative runs on `model`; everything else on the provider's auxiliary model."""
    narrative_model = _OPENAI_MODEL if provider == LLMProvider.OPENAI else model
    _llm.config.primary_provider = provider
    _llm.config.primary_model = _AUXILIARY_MODELS.get(provider, narrative_model)
    _llm.config.orchestrator_model = narrative_model


_narrator = NarratorEngine(llm=_llm)
# Role routing for every model call made from the play loop. Telemetry lands in
# the game-state database (falling back to the scenario database), so the dev
# panel can show which model answered, how long it took and why it failed.
# The store is resolved lazily on first use: the play-loop database may not
# exist yet at import time.
_memory = MemoryEngine(event_store=_event_store, llm=_llm)
_world_reactor = WorldReactor(llm=_llm)
_journal = JournalEngine(llm=_llm, event_store=_event_store)
_combat = CombatEngine(llm=_llm)
_plot_generator = PlotGenerator(llm=_llm)
_npc_minds = NpcMindEngine(llm=_llm)
_inventory = InventoryEngine(event_store=_event_store)

_sessions: dict[str, GameSession] = {}

#: Telemetry lives in the play-loop database that actually exists on this host.
_telemetry_store = None


def _record_agent_call(**fields):
    """Persist one agent call; telemetry must never break a turn."""
    try:
        store = _agent_telemetry_store()
        if store is not None:
            store.record_agent_call(**fields)
    except Exception:  # noqa: BLE001 - observability is not a game rule
        logger.debug("agent call not recorded", exc_info=True)


def _agent_telemetry_store():
    """A store that can record agent calls, opened lazily and kept open.

    The scenario database is always present (it holds the campaign), while the
    game-state database only appears once a campaign has been bound; either can
    answer the query, so the first one that opens wins.
    """
    global _telemetry_store
    if _telemetry_store is None:
        for factory in (lambda: ScenarioStore(_scenario_db_path()), lambda: GameStateStore(_game_state_db_path())):
            try:
                store = factory()
            except Exception:  # noqa: BLE001 - telemetry is optional
                continue
            if hasattr(store, "record_agent_call"):
                _telemetry_store = store
                break
    return _telemetry_store


_agents = AgentRuntime(_llm, record=_record_agent_call)



_graph_engines: dict = {}

def _scenario_db_path() -> str:
    """Resolve the scenario database per request so tests and env changes apply."""
    return os.environ.get("SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db"))


def _load_story_cards_for_campaign(campaign_id: str):
    """Look up the scenario_id from the campaign and load its story cards."""
    try:
        store = ScenarioStore(_scenario_db_path())
        # Find the campaign's scenario_id
        conn = store._conn
        row = conn.execute(
            "SELECT scenario_id FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
        if not row:
            return []
        scenario_id = row[0]
        return store.get_story_cards(scenario_id)
    except Exception:
        logger.debug("Could not load story cards for campaign %s", campaign_id)
        return []


def _load_knowledge_context_for_campaign(campaign_id: str) -> str:
    """Assemble authored world knowledge for the narrator prompt (best effort).

    Knowledge is configuration, not game state: the live narration loop may
    read it but never let it decide facts. A failure here must never block
    play, so every error degrades to "no knowledge".
    """
    try:
        with ScenarioStore(_scenario_db_path()) as store:
            campaign = store.get_campaign(campaign_id)
            if not campaign:
                return ""
            scenario_id = campaign.scenario_id
            entries = store.list_knowledge(scenario_id)
            config = store.get_play_config(scenario_id)
        if not entries:
            return ""
        from app.models.knowledge import LAYER_LABELS
        from app.services.knowledge_service import build_context

        focus: list[str] = []
        try:
            from app.engines.event_engine import condition_holds, entity_location
            from app.models.game import Condition

            with GameStateStore(_game_state_db_path()) as game_store:
                bound_state = game_store.get_state(campaign_id)
                bound_world = (
                    game_store.get_world(bound_state.world_id) if bound_state else None
                )
            if bound_state is not None and bound_state.location_id:
                focus.append(bound_state.location_id)
                if bound_world is not None:
                    for entity in bound_world.entities:
                        if entity.kind == "object":
                            continue
                        if entity_location(bound_state, bound_world, entity.id) != bound_state.location_id:
                            continue
                        if condition_holds(
                            bound_state, bound_world,
                            Condition(kind="entity_alive", target=entity.id),
                        ):
                            focus.append(entity.id)
        except Exception:  # noqa: BLE001 - focus is advisory
            focus = []
        context = build_context(
            entries,
            mode=config.mode,
            max_depth=config.max_depth,
            scenario_id=scenario_id,
            focus=focus or None,
            budget_chars=config.context_budget_chars,
        )
        blocks = context.prompt_blocks()
        if not blocks:
            return ""
        lines = []
        for block in blocks:
            label = LAYER_LABELS.get(block["layer"], block["layer"])
            lines.append(f"[{label}] {block['title']}\n{block['content']}")
        return "\n\n".join(lines)
    except Exception:
        logger.debug("Could not load world knowledge for campaign %s", campaign_id)
        return ""


def _load_scenario_for_campaign(campaign_id: str):
    """Load scenario tone, language, and opening narrative for a campaign."""
    try:
        store = ScenarioStore(_scenario_db_path())
        conn = store._conn
        row = conn.execute(
            "SELECT s.tone_instructions, s.language, s.opening_narrative "
            "FROM scenarios s JOIN campaigns c ON c.scenario_id = s.id "
            "WHERE c.id=?",
            (campaign_id,),
        ).fetchone()
        if not row:
            return "", "en", ""
        return row[0] or "", row[1] or "en", row[2] or ""
    except Exception:
        logger.debug("Could not load scenario for campaign %s", campaign_id)
        return "", "en", ""


def _load_setup_answers_for_campaign(campaign_id: str) -> dict:
    """Load setup wizard answers persisted for the campaign (empty if none)."""
    try:
        with ScenarioStore(_scenario_db_path()) as store:
            campaign = store.get_campaign(campaign_id)
        return campaign.setup_answers if campaign else {}
    except Exception:
        logger.debug("Could not load setup answers for campaign %s", campaign_id)
        return {}


def _ensure_session(campaign_id: str) -> GameSession:
    """Get or create a GameSession, ensuring all in-memory state is rebuilt.

    This is used by GET endpoints that need rebuilt data (NPC minds, journal,
    crystals, inventory) without requiring a player action first.
    """
    if campaign_id in _sessions:
        return _sessions[campaign_id]

    tone, language, opening = _load_scenario_for_campaign(campaign_id)
    story_cards = _load_story_cards_for_campaign(campaign_id)
    setup_answers = _load_setup_answers_for_campaign(campaign_id)
    knowledge_context = _load_knowledge_context_for_campaign(campaign_id)
    combat_enabled = True
    # If the campaign has an AI-generated opening, prefer it over the
    # scenario template — that's the text the player saw in the UI.
    try:
        with ScenarioStore(_scenario_db_path()) as _store:
            _campaign = _store.get_campaign(campaign_id)
        if _campaign:
            if _campaign.generated_opening:
                opening = _campaign.generated_opening
            combat_enabled = _campaign.combat_enabled
    except Exception:
        logger.debug("Could not load campaign metadata for %s", campaign_id)
    graph = _get_graph_engine(campaign_id)
    graphiti = _get_graphiti_engine()
    if graphiti:
        _memory.set_graphiti(graphiti)

    _sessions[campaign_id] = GameSession(
        campaign_id=campaign_id,
        scenario_tone=tone,
        language=language,
        narrator=_narrator,
        memory=_memory,
        world_reactor=_world_reactor,
        journal=_journal,
        event_store=_event_store,
        combat_engine=_combat,
        graph_engine=graph,
        npc_minds=_npc_minds,
        graphiti_engine=graphiti,
        plot_generator=_plot_generator,
        inventory_engine=_inventory,
        opening_narrative=opening,
        story_cards=story_cards,
        setup_answers=setup_answers,
        combat_enabled=combat_enabled,
        knowledge_context=knowledge_context,
        knowledge_provider=lambda: _load_knowledge_context_for_campaign(campaign_id),
        agents=_agents,
    )
    return _sessions[campaign_id]

_graphiti_engine = None


def _get_graphiti_engine():
    global _graphiti_engine
    if _graphiti_engine is not None:
        return _graphiti_engine
    try:
        from app.engines.graphiti_engine import GraphitiEngine
        _graphiti_engine = GraphitiEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        return _graphiti_engine
    except Exception:
        logger.info("Graphiti not available, running without temporal graph")
        return None


async def _fallback_graph_search(campaign_id: str, query: str, limit: int = 10) -> list[dict]:
    """Fallback keyword search over Neo4j world graph when Graphiti has no results."""
    graph = _get_graph_engine(campaign_id)
    if not graph:
        return []

    try:
        await graph.initialize()
        nodes = await graph.get_all_nodes()
        relationships = await graph.get_all_relationships()
    except Exception:
        logger.warning("Fallback world graph search failed for campaign %s", campaign_id, exc_info=True)
        return []

    term = query.strip().lower()
    if not term:
        return []

    node_lookup = {getattr(n, "id", ""): n for n in nodes}
    matched_nodes = []
    for node in nodes:
        name = str(getattr(node, "name", ""))
        attrs = getattr(node, "attributes", {}) or {}
        attr_text = " ".join(str(v) for v in attrs.values())
        haystack = f"{name} {attr_text}".lower()
        if term in haystack:
            matched_nodes.append(node)

    facts: list[dict] = []
    seen: set[str] = set()
    for node in matched_nodes:
        node_type = getattr(node, "node_type", None)
        node_type_value = node_type.value if hasattr(node_type, "value") else str(node_type or "NODE")
        node_fact = f"{getattr(node, 'name', 'Unknown')} [{node_type_value}]"
        if node_fact not in seen:
            facts.append({"fact": node_fact, "valid_at": None, "invalid_at": None})
            seen.add(node_fact)
            if len(facts) >= limit:
                break

        for rel in relationships:
            source = node_lookup.get(rel.get("source_id"))
            target = node_lookup.get(rel.get("target_id"))
            if not source or not target:
                continue
            if getattr(source, "id", None) != getattr(node, "id", None) and getattr(target, "id", None) != getattr(node, "id", None):
                continue

            rel_fact = (
                f"{getattr(source, 'name', 'Unknown')} "
                f"-{rel.get('rel_type', 'RELATED_TO')}-> "
                f"{getattr(target, 'name', 'Unknown')}"
            )
            if rel_fact in seen:
                continue

            facts.append({"fact": rel_fact, "valid_at": None, "invalid_at": None})
            seen.add(rel_fact)
            if len(facts) >= limit:
                break
        if len(facts) >= limit:
            break

    return facts[:limit]


class PlayerActionRequest(BaseModel):
    campaign_id: str = Field(..., min_length=1, max_length=64)
    scenario_tone: str = Field(default="", max_length=50000)
    language: str = Field(default="en", max_length=10)
    action: str = Field(..., min_length=1, max_length=20000)
    opening_narrative: str = Field(default="", max_length=50000)
    max_tokens: int = Field(default=2000, ge=256, le=8192)
    provider: str = Field(default="deepseek", max_length=20)
    model: str = Field(default="deepseek-v4-flash", max_length=64)
    temperature: float = Field(default=0.85, ge=0.0, le=2.0)
    combat_enabled: bool | None = None


# ---------------------------------------------------------------- unified lane
# The chat surface (/api/game/action) and the structured surface
# (/api/v1/campaigns/{id}/actions) share ONE authority: ActionService. When a
# campaign has a rules world, a typed utterance is parsed into the same
# ActionRequest, decided by the same rule engine, committed in the same
# transaction and only then narrated. Campaigns without a world keep the legacy
# LLM-driven session, where prose can no longer mutate items (see
# app/engines/item_proposals.py).


def _context_window_quietly(campaign_id: str) -> str:
    """Build the world-memory window, tolerating a store that cannot be read.

    Generation endpoints and timeskip treat memory as enrichment: on a fresh
    or unreadable event store the turn still has to run, so the window just
    comes back empty instead of turning the request into a 500.
    """
    try:
        return _memory.build_context_window(campaign_id)
    except Exception:
        logger.warning("world memory window unavailable for %s", campaign_id, exc_info=True)
        return ""


def _game_state_db_path() -> str:
    return os.environ.get(
        "GAME_STATE_DB_PATH", os.path.join(_BACKEND_DIR, "game_state.db")
    )


def _api_key_configured(provider: LLMProvider) -> bool:
    if provider == LLMProvider.LOCAL:
        return bool(settings.local_llm_api_key or os.environ.get("LOCAL_LLM_API_KEY"))
    if provider == LLMProvider.DEEPSEEK:
        return bool(settings.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY"))
    if provider == LLMProvider.OPENAI:
        return bool(settings.openai_api_key or os.environ.get("OPENAI_API_KEY"))
    return bool(settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY"))


def _unified_narrator(language: str, max_tokens: int):
    """Narrator for the chat lane, decided by the shared factory.

    `auto` uses the model only when one is actually reachable-by-configuration:
    a local NInfer server counts even with no cloud key, which is the normal
    setup on this machine. The router is per-narrator for the local provider so
    the request-scoped model policy applied to `_llm` cannot redirect it.
    """
    from app.services import narrator_factory

    if narrator_factory.narrator_choice("ENGINE_UNIFIED_NARRATOR") == "template":
        return TemplateNarrator()
    try:
        if narrator_factory.prefer_local():
            narrator, _provider = narrator_factory.build_narrator(
                "ENGINE_UNIFIED_NARRATOR",
                language=language or "zh",
                max_tokens=max(256, min(int(max_tokens or 600), 900)),
                temperature=0.9,
                timeout=60.0,
            )
            return narrator
        return LLMNarrator(
            _llm,
            max_tokens=max(256, min(int(max_tokens or 600), 900)),
            temperature=0.9,
            timeout=60.0,
            language=language or "zh",
        )
    except Exception:  # noqa: BLE001 - narrator setup must never block play
        logger.warning("Unified narrator setup failed; using template", exc_info=True)
        return TemplateNarrator()


def _knowledge_provider_for(scenario_store: ScenarioStore, campaign_id: str):
    """Read campaign-aware authored context without changing narration authority."""
    from app.services.narrative_service import scenario_play_context

    def provider(scenario_id: str):
        return scenario_play_context(scenario_store, scenario_id, campaign_id)

    return provider


def _unified_service(req: PlayerActionRequest):
    """Build the rules-lane service, or None when the campaign has no world."""
    game_store = GameStateStore(_game_state_db_path())
    scenario_store = ScenarioStore(_scenario_db_path())
    try:
        service = TextActionService(game_store, scenario_store=scenario_store)
        state = game_store.get_state(req.campaign_id)
        try:
            if state is not None:
                world = game_store.get_world(state.world_id)
            else:
                # Binds an authored world for the scenario, or creates a
                # validated starter one, so a new campaign never leaves the
                # rules lane (the legacy session cannot see this state).
                world = service.bindable_world(req.campaign_id)
        except LookupError:
            world = None
        if world is None and state is not None:
            # The campaign keeps its progress; only the world document went away.
            try:
                world = service._world_for_state(state, req.campaign_id)
            except LookupError:
                world = None
        if world is None:
            game_store.close()
            scenario_store.close()
            return None
        service.action_service = ActionService(
            game_store,
            narrator=_unified_narrator(req.language or "zh", req.max_tokens),
            context_provider=_knowledge_provider_for(scenario_store, req.campaign_id),
        )
        return service, scenario_store
    except Exception:
        game_store.close()
        scenario_store.close()
        logger.warning(
            "Unified action lane unavailable for campaign %s", req.campaign_id,
            exc_info=True,
        )
        return None


def _mirror_unified_turn(req, before, result: dict) -> None:
    """Project one committed rules turn into the legacy readable timeline.

    These events are a *view* of the authoritative state, written after the
    transaction committed. They let the existing UI, journal and context
    rebuild keep working without giving them any authority.
    """
    try:
        state = result.get("state") or {}
        narrative = result.get("narrative") or {}
        text = narrative.get("text") or ""
        _event_store.append(
            campaign_id=req.campaign_id,
            event_type=EventType.PLAYER_ACTION,
            payload={"text": req.action},
            narrative_time_delta=0,
            location=state.get("location_id") or "current",
            entities=[],
        )
        if text:
            _event_store.append(
                campaign_id=req.campaign_id,
                event_type=EventType.NARRATOR_RESPONSE,
                payload={"text": text},
                narrative_time_delta=0,
                location=state.get("location_id") or "current",
                entities=[],
            )
        world_id = state.get("world_id")
        world = _get_state_world(world_id) if world_id else None
        if world is None:
            return
        names = {item.id: item.name for item in world.items}
        before_inv = dict(before.inventory) if before is not None else {}
        after_inv = dict(state.get("inventory") or {})
        for item_id, count in after_inv.items():
            delta = int(count) - int(before_inv.get(item_id, 0))
            name = names.get(item_id, item_id)
            if delta > 0:
                _inventory.add_item(req.campaign_id, name, "rules", "确定性规则")
            elif delta < 0:
                _inventory.lose_item(req.campaign_id, name)
        for item_id, count in before_inv.items():
            if item_id not in after_inv and int(count) > 0:
                _inventory.lose_item(req.campaign_id, names.get(item_id, item_id))
    except Exception:  # noqa: BLE001 - the turn is already committed
        logger.warning("Could not mirror unified turn events", exc_info=True)


def _get_state_world(world_id: str):
    try:
        store = GameStateStore(_game_state_db_path())
        try:
            return store.get_world(world_id)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 - projection only
        return None


async def _unified_event_stream(req: PlayerActionRequest, service, scenario_store):
    """SSE adapter: commit first, then describe, then stream one narrative."""
    reset_call_log()
    game_store = service.game_store
    try:
        before = game_store.get_state(req.campaign_id)
        try:
            payload = await service.execute(req.campaign_id, req.action)
        except LookupError as exc:
            yield f"data: 无法继续：{exc}\n\n"
            yield "data: [DONE]\n\n"
            return
        result = payload.get("result") or {}
        narrative = result.get("narrative") or {}
        text = (narrative.get("text") or "").strip()
        if not text:
            if narrative.get("status") == "failed":
                detail = narrative.get("error") or "未知错误"
                text = (
                    f"（本回合叙事生成失败：{detail}。行动结果与状态已经提交，"
                    f"当前状态版本 {result.get('state_version')}。）"
                )
            elif result.get("status") == "rejected":
                text = result.get("message") or "这件事没有发生。"
            elif result.get("status") == "duplicate":
                text = "这条行动之前已经处理过，本次没有产生新的变化。"
        if text:
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                yield f"data: {line}\n"
            yield "\n"
        _mirror_unified_turn(req, before, result)
        summary = get_call_summary()
        yield f"data: [USAGE]{json.dumps(summary)}\n\n"
        trace = get_call_trace()
        if trace:
            try:
                _trace_store.append(req.campaign_id, action=req.action, entries=trace, summary=summary)
            except Exception:
                logger.exception("Failed to persist LLM trace for campaign %s", req.campaign_id)
        yield "data: [DONE]\n\n"
    finally:
        try:
            game_store.close()
        finally:
            scenario_store.close()


class SettingsRequest(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    temperature: float = 0.85
    max_tokens: int = 2000


class GenerateRequest(BaseModel):
    type: str  # "npc", "event", "plot"
    language: str = "en"


class TimeskipRequest(BaseModel):
    seconds: int


class NpcMindUpdateRequest(BaseModel):
    thoughts: dict[str, str]  # {thought_key: value}


class InventoryActionRequest(BaseModel):
    name: str
    action: str  # "use" or "discard"


class SetupAnswer(BaseModel):
    var_name: str
    resolved_prompt: str = ""
    type: str  # "text" | "choice"
    value: str
    description: str = ""


class SetupAnswersRequest(BaseModel):
    answers: dict[str, SetupAnswer]


class CampaignSettingsRequest(BaseModel):
    combat_enabled: bool


def _get_graph_engine(campaign_id: str):
    """Get or create a GraphEngine for the given campaign."""
    if campaign_id in _graph_engines:
        return _graph_engines[campaign_id]
    try:
        from app.engines.graph_engine import GraphEngine
        engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password, campaign_id)
        _graph_engines[campaign_id] = engine
        return engine
    except Exception:
        logger.info("GraphEngine not available for campaign %s", campaign_id)
        return None


@router.post("/action")
async def player_action(req: PlayerActionRequest):
    # Apply the user's LLM settings per-request first: both lanes narrate with
    # the same router, and the unified lane has no legacy session to configure.
    try:
        provider = LLMProvider(req.provider)
    except ValueError:
        provider = _llm.config.primary_provider
    apply_model_policy(provider, req.model)
    _llm.config.temperature = req.temperature
    _llm.config.max_tokens = req.max_tokens

    # A campaign that has a rules world plays through the unified lane: the
    # utterance is parsed, decided and committed by the rule engine, and only
    # then narrated. Campaigns without a world keep the legacy LLM session.
    unified = _unified_service(req)
    if unified is not None:
        service, scenario_store = unified
        # Drop the cached legacy session so returning to it rebuilds from events.
        _sessions.pop(req.campaign_id, None)
        return StreamingResponse(
            _unified_event_stream(req, service, scenario_store),
            media_type="text/event-stream",
        )

    session = _ensure_session(req.campaign_id)
    # Update session with request-specific values that may differ per action.
    # The frontend may send the raw tone template — re-interpolate against the
    # session's setup answers so {var} tokens never reach the narrator.
    if req.scenario_tone:
        session.scenario_tone = interpolate(
            req.scenario_tone, session._setup_answers,
            context=f"campaign:{req.campaign_id}:tone",
        )
    session.language = req.language or session.language
    if req.combat_enabled is not None:
        session.set_combat_enabled(req.combat_enabled)

    async def event_stream():
        reset_call_log()
        async for chunk in session.process_action(req.action, max_tokens=req.max_tokens):
            # SSE requires each data line to be prefixed with "data:".
            # This preserves paragraph breaks/newlines in streamed prose.
            text = str(chunk).replace("\r\n", "\n").replace("\r", "\n")
            for line in text.split("\n"):
                yield f"data: {line}\n"
            yield "\n"
        # Per-turn token summary. Reflects the synchronous calls drained by the
        # stream (detect_mode + narrator, ~90% of the turn's tokens); fire-and-forget
        # side effects log separately and are not in this snapshot.
        summary = get_call_summary()
        logger.warning(
            "📊 ACTION COMPLETE: %d LLM calls, %d input, %d output, "
            "%d cache_read, %d cache_creation, %.1fs total",
            summary["call_count"], summary["total_input_tokens"],
            summary["total_output_tokens"], summary["total_cache_read_tokens"],
            summary["total_cache_creation_tokens"], summary["total_time_s"],
        )
        # Ride the existing SSE control-tag channel to the frontend devtools readout.
        yield f"data: [USAGE]{json.dumps(summary)}\n\n"
        # Full per-call trace (input sections + output) so the devtools panel can
        # show exactly what each synchronous call sent. json.dumps escapes newlines,
        # so the whole payload stays on one SSE data line.
        trace = get_call_trace()
        if trace:
            try:
                _trace_store.append(req.campaign_id, action=req.action, entries=trace, summary=summary)
            except Exception:
                logger.exception("Failed to persist LLM trace for campaign %s", req.campaign_id)
        yield f"data: [TRACE]{json.dumps(trace, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/{campaign_id}/scenario-view")
async def get_scenario_view(campaign_id: str):
    """Return the resolved per-campaign scenario surface (opening, tone, lore).

    Precedence for ``opening_narrative``:
      1. ``campaign.generated_opening`` if non-empty (AI-generated path).
      2. ``scenario.opening_narrative`` interpolated against setup_answers.
    """
    with ScenarioStore(_scenario_db_path()) as store:
        campaign = store.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        scenario = store.get_scenario(campaign.scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    answers = campaign.setup_answers or {}
    ctx_root = f"campaign:{campaign_id}"
    opening = campaign.generated_opening or interpolate(
        scenario.opening_narrative, answers, context=f"{ctx_root}:opening",
    )
    from app.api.routes_characters import _character_dict

    with ScenarioStore(_scenario_db_path()) as store:
        characters = [_character_dict(c) for c in store.list_characters(scenario.id)]

    return {
        "title": scenario.title,
        "language": scenario.language,
        "world_type": scenario.world_type,
        "characters": characters,
        "opening_narrative": opening,
        "tone_instructions": interpolate(
            scenario.tone_instructions, answers, context=f"{ctx_root}:tone",
        ),
        "lore_text": interpolate(
            scenario.lore_text, answers, context=f"{ctx_root}:lore",
        ),
        "opening_mode": scenario.opening_mode,
        "has_generated_opening": bool(campaign.generated_opening),
        "combat_enabled": campaign.combat_enabled,
    }


@router.get("/{campaign_id}/setup-state")
async def get_setup_state(campaign_id: str):
    """Return the wizard state for a campaign: questions defined by the scenario,
    answers already saved, and whether the wizard still needs to run."""
    with ScenarioStore(_scenario_db_path()) as store:
        campaign = store.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        scenario = store.get_scenario(campaign.scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    questions = scenario.setup_questions or []
    answers = campaign.setup_answers or {}
    needs_setup = bool(questions) and not answers
    return {
        "questions": questions,
        "answers": answers,
        "needs_setup": needs_setup,
    }


async def _maybe_generate_opening(
    campaign_id: str,
    setup_payload: dict,
) -> str | None:
    """If the scenario opts into AI openings, generate one and persist it.

    Returns the generated text on success, ``None`` if the scenario is in
    fixed mode or generation failed (the caller falls back to the static
    template). Any LLM failure is logged but never raised — players should
    not be blocked by an outage at session start.
    """
    import time

    with ScenarioStore(_scenario_db_path()) as store:
        campaign = store.get_campaign(campaign_id)
        if not campaign:
            return None
        scenario = store.get_scenario(campaign.scenario_id)
    if not scenario or scenario.opening_mode != "ai":
        return None

    lines = format_setup_lines(setup_payload, scenario.setup_questions)
    router_ = LLMRouter(LLMConfig())
    t0 = time.monotonic()
    try:
        text = await generate_opening(
            language=scenario.language,
            tone=scenario.tone_instructions,
            lore=scenario.lore_text,
            character_setup_lines=lines,
            director_note=scenario.ai_opening_directive,
            router=router_,
        )
    except Exception:
        logger.exception(
            "AI opening generation failed for campaign %s — falling back to template",
            campaign_id,
        )
        return None

    duration_ms = int((time.monotonic() - t0) * 1000)
    if not text:
        return None

    with ScenarioStore(_scenario_db_path()) as store:
        store.update_generated_opening(campaign_id, text)

    try:
        _event_store.append(
            campaign_id=campaign_id,
            event_type=EventType.AI_OPENING_GENERATED,
            payload={
                "char_count": len(text),
                "model": router_.config.primary_model,
                "duration_ms": duration_ms,
            },
            narrative_time_delta=0,
            location="meta",
            entities=[],
        )
    except Exception:
        logger.debug("Could not log AI_OPENING_GENERATED event", exc_info=True)
    return text


@router.post("/{campaign_id}/pause")
async def pause_campaign(campaign_id: str):
    """Pause a campaign: the player has left, so nothing advances.

    The legacy session holds fire-and-forget world ticks; pausing cancels
    them and refuses further turns. Nothing is ever computed from wall-clock
    time, so there is no offline catch-up to undo on resume.
    """
    session = _sessions.get(campaign_id)
    if session is not None:
        session.pause()
    return {"status": "ok", "campaign_id": campaign_id, "paused": True, "session_cached": session is not None}


@router.post("/{campaign_id}/resume")
async def resume_campaign(campaign_id: str):
    """Resume a campaign exactly where the player left it.

    The session is rebuilt from stored events rather than carried in memory,
    which is what makes "no offline time passed" true by construction.
    """
    session = _sessions.get(campaign_id)
    if session is not None:
        session.resume()
    else:
        _sessions.pop(campaign_id, None)
    return {"status": "ok", "campaign_id": campaign_id, "paused": False}


def campaign_clock_paused(campaign_id: str) -> bool:
    """Whether this campaign's world clock is frozen.

    A campaign with no cached session counts as paused: if nobody is playing
    it, nothing in it moves. This is the single predicate the rules pipeline
    consults before it lets a turn advance the clock.
    """
    session = _sessions.get(campaign_id)
    if session is None:
        return True
    return bool(getattr(session, "paused", False))


@router.get("/{campaign_id}/clock")
async def get_campaign_clock(campaign_id: str):
    """Report the in-game clock, and whether it is running, paused or idle."""
    session = _sessions.get(campaign_id)
    paused = campaign_clock_paused(campaign_id)
    clock = None
    try:
        from app.engines.game_clock import Clock
        from app.db.game_state_store import GameStateStore

        with GameStateStore(_game_state_db_path(), migrate=False) as store:
            state = store.get_state(campaign_id)
        if state is not None:
            clock = Clock.from_state(state).to_dict()
    except Exception:  # noqa: BLE001 - the clock view is informational
        clock = None
    return {
        "campaign_id": campaign_id,
        "state": "paused" if paused and session is not None else (
            "running" if session is not None else "idle"
        ),
        "session_cached": session is not None,
        "clock_paused": paused,
        "clock": clock,
    }


@router.patch("/{campaign_id}/settings")
async def update_campaign_settings(campaign_id: str, req: CampaignSettingsRequest):
    """Persist campaign-level toggles (e.g. combat mode on/off)."""
    with ScenarioStore(_scenario_db_path()) as store:
        ok = store.update_combat_enabled(campaign_id, req.combat_enabled)
    if not ok:
        raise HTTPException(status_code=404, detail="Campaign not found")
    session = _sessions.get(campaign_id)
    if session:
        session.set_combat_enabled(req.combat_enabled)
    return {"status": "ok", "combat_enabled": req.combat_enabled}


@router.post("/{campaign_id}/setup-answers")
async def save_setup_answers(campaign_id: str, req: SetupAnswersRequest):
    """Persist the wizard answers and refresh the active session so the
    CHARACTER SETUP block is injected on the next action."""
    payload = {k: v.model_dump() for k, v in req.answers.items()}
    with ScenarioStore(_scenario_db_path()) as store:
        ok = store.update_setup_answers(campaign_id, payload)
    if not ok:
        raise HTTPException(status_code=404, detail="Campaign not found")

    generated = await _maybe_generate_opening(campaign_id, payload)

    # Drop the cached session so _ensure_session re-reads setup_answers next call.
    _sessions.pop(campaign_id, None)
    return {
        "status": "ok",
        "answers": payload,
        "generated_opening": generated or "",
    }


@router.post("/{campaign_id}/regenerate-opening")
async def regenerate_opening(campaign_id: str):
    """Re-roll the AI-generated opening for an as-yet-untouched campaign.

    Locked once any narrator turn exists for the campaign — re-rolling the
    opening mid-story would break the AI's history continuity.
    """
    with ScenarioStore(_scenario_db_path()) as store:
        campaign = store.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        scenario = store.get_scenario(campaign.scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if scenario.opening_mode != "ai":
        raise HTTPException(
            status_code=400,
            detail="Scenario is not in AI opening mode.",
        )

    existing = _event_store.get_by_type(campaign_id, EventType.NARRATOR_RESPONSE)
    if existing:
        raise HTTPException(
            status_code=409,
            detail="Cannot re-roll the opening after the story has begun.",
        )

    generated = await _maybe_generate_opening(campaign_id, campaign.setup_answers or {})
    if not generated:
        raise HTTPException(
            status_code=502,
            detail="Opening generation failed. Try again or check API keys.",
        )

    # Drop the cached session so the new opening seeds the next history rebuild.
    _sessions.pop(campaign_id, None)
    return {"status": "ok", "opening_narrative": generated}


@router.get("/{campaign_id}/history")
async def get_history(campaign_id: str):
    """Rebuild bound campaigns from engine records, never their legacy mirrors."""
    with GameStateStore(_game_state_db_path()) as store:
        if store.get_state(campaign_id) is not None:
            return {"messages": [
                {
                    "role": "user" if event["event_type"] == "PLAYER_ACTION" else "assistant",
                    "content": event["payload"]["text"],
                }
                for event in store.narrative_history(campaign_id)
            ]}
    events = _event_store.get_by_type(campaign_id, EventType.PLAYER_ACTION) + \
             _event_store.get_by_type(campaign_id, EventType.NARRATOR_RESPONSE)
    events.sort(key=lambda e: e.created_at)
    messages = []
    for ev in events:
        text = ev.payload.get("text", "")
        if ev.event_type == EventType.PLAYER_ACTION:
            messages.append({"role": "user", "content": text})
        else:
            messages.append({"role": "assistant", "content": text})
    return {"messages": messages}


@router.get("/{campaign_id}/traces")
async def get_traces(campaign_id: str, limit: int = 25):
    """Return persisted LLM call traces for the devtools panel."""
    try:
        return {"traces": _trace_store.get_recent(campaign_id, limit)}
    except Exception:
        logger.exception("Failed to load LLM traces for campaign %s", campaign_id)
        return {"traces": []}


@router.delete("/{campaign_id}/traces")
async def delete_traces(campaign_id: str):
    """Delete all persisted LLM call traces for a campaign."""
    return {"deleted": _trace_store.delete_for_campaign(campaign_id)}


@router.post("/{campaign_id}/rewind")
async def rewind(campaign_id: str):
    """Legacy rewind must not diverge a bound campaign's chat from engine state."""
    with GameStateStore(_game_state_db_path()) as store:
        if store.get_state(campaign_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Engine campaigns cannot use legacy rewind. Use the authorized "
                    "debug rollback endpoint /api/v1/campaigns/"
                    f"{campaign_id}/rollback (requires ENGINE_DEBUG_TOOLS)."
                ),
            )
    deleted = _event_store.delete_last_pair(campaign_id)
    if deleted == 0:
        raise HTTPException(status_code=404, detail="No actions to rewind")

    # If there's a live session, rewind its in-memory state
    if campaign_id in _sessions:
        _sessions[campaign_id].rewind()

    # Return updated message history (same format as GET /history)
    events = _event_store.get_by_type(campaign_id, EventType.PLAYER_ACTION) + \
             _event_store.get_by_type(campaign_id, EventType.NARRATOR_RESPONSE)
    events.sort(key=lambda e: e.created_at)
    messages = []
    for ev in events:
        text = ev.payload.get("text", "")
        if ev.event_type == EventType.PLAYER_ACTION:
            messages.append({"role": "user", "content": text})
        else:
            messages.append({"role": "assistant", "content": text})
    return {"messages": messages, "deleted": deleted}


@router.get("/{campaign_id}/journal")
async def get_journal(campaign_id: str, category: str | None = None):
    # Journal reads stay pure: a rebuild would drop entries that were logged
    # in this process but not (yet) persisted — including entries added by an
    # in-flight session, which the player can already see in the UI.
    if category:
        try:
            cat = JournalCategory(category)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid journal category: {category}")
        entries = _journal.get_by_category(campaign_id, cat)
    else:
        entries = _journal.get_journal(campaign_id)
    return [asdict(e) for e in entries]


@router.get("/{campaign_id}/npc-minds")
async def get_npc_minds(campaign_id: str):
    _ensure_session(campaign_id)
    minds = _npc_minds.get_all_minds(campaign_id)
    return [m.to_dict() for m in minds]


@router.delete("/{campaign_id}/npc-minds/{npc_name}")
async def delete_npc_mind(campaign_id: str, npc_name: str):
    """Delete an NPC mind from memory and event store."""
    _ensure_session(campaign_id)
    deleted = _npc_minds.delete_mind(campaign_id, npc_name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"NPC '{npc_name}' not found")
    _event_store.delete_npc_thoughts(campaign_id, npc_name)
    return {"status": "deleted", "name": npc_name}


@router.put("/{campaign_id}/npc-minds/{npc_name}")
async def update_npc_mind(campaign_id: str, npc_name: str, req: NpcMindUpdateRequest):
    """Update thoughts for an NPC mind."""
    _ensure_session(campaign_id)
    mind = None
    for key, value in req.thoughts.items():
        mind = _npc_minds.update_thought(campaign_id, npc_name, key, value)
    if not mind:
        raise HTTPException(status_code=404, detail=f"NPC '{npc_name}' not found")
    # Persist updated thoughts to event store
    thoughts_dict = {k: t.value for k, t in mind.thoughts.items()}
    _event_store.upsert_npc_thought(campaign_id, mind.name, thoughts_dict, mind.aliases)
    return mind.to_dict()


@router.get("/{campaign_id}/characters")
async def get_characters(campaign_id: str, q: str = ""):
    """List all known characters/entities for @-mention autocomplete.

    Returns NPCs from NpcMindEngine + entities from GraphEngine (if available).
    Optional query parameter `q` filters by substring match.
    """
    _ensure_session(campaign_id)
    characters: list[dict] = []
    seen_names: set[str] = set()

    # NPCs from mind engine (primary source — has thoughts and aliases)
    for mind in _npc_minds.get_all_minds(campaign_id):
        name_lower = mind.name.lower()
        if name_lower in seen_names:
            continue
        seen_names.add(name_lower)
        characters.append({
            "name": mind.name,
            "aliases": mind.aliases,
            "type": "NPC",
        })

    # Scenario character pack (authored portraits and roles)
    with ScenarioStore(_scenario_db_path()) as store:
        campaign = store.get_campaign(campaign_id)
        pack = store.list_characters(campaign.scenario_id) if campaign else []
    for character in pack:
        if character.name.lower() in seen_names:
            continue
        seen_names.add(character.name.lower())
        characters.append({
            "name": character.name,
            "aliases": [],
            "type": "PLAYER" if character.is_player else "NPC",
            "description": character.description,
            "portrait_url": character.portrait_url,
        })

    # Entities from graph engine (if available)
    session = _sessions.get(campaign_id)
    if session and hasattr(session, "_graph") and session._graph:
        try:
            nodes = await session._graph.get_all_nodes()
            for node in nodes:
                name_lower = node.name.lower()
                if name_lower in seen_names:
                    continue
                seen_names.add(name_lower)
                characters.append({
                    "name": node.name,
                    "aliases": [],
                    "type": node.node_type.value if hasattr(node.node_type, "value") else str(node.node_type),
                })
        except Exception:
            pass

    # Filter by query if provided
    if q:
        q_lower = q.lower()
        characters = [
            c for c in characters
            if q_lower in c["name"].lower()
            or any(q_lower in a.lower() for a in c.get("aliases", []))
        ]

    return characters


@router.get("/{campaign_id}/memory-crystals")
async def get_memory_crystals(campaign_id: str):
    _ensure_session(campaign_id)
    crystals = _memory.get_crystals(campaign_id)
    return [
        {
            "tier": c.tier.value,
            "content": c.content,
            "event_count": c.event_count,
        }
        for c in crystals
    ]


@router.post("/{campaign_id}/crystallize")
async def crystallize_memory(campaign_id: str):
    crystal = await _memory.crystallize(campaign_id, CrystalTier.SHORT)
    return {
        "tier": crystal.tier.value,
        "content": crystal.content,
        "event_count": crystal.event_count,
    }


@router.post("/{campaign_id}/generate")
async def generate_content(campaign_id: str, req: GenerateRequest):
    world_ctx = _context_window_quietly(campaign_id)
    if req.type == "npc":
        npc = await _plot_generator.generate_npc(world_ctx, language=req.language)
        return asdict(npc)
    elif req.type == "event":
        total_time = _event_store.get_total_narrative_time(campaign_id)
        event = await _plot_generator.generate_random_event("current", world_ctx, total_time, language=req.language)
        return asdict(event)
    elif req.type == "plot":
        arc = await _plot_generator.generate_plot_arc(world_ctx, language=req.language)
        return {"text": arc}
    return {"error": "Unknown type"}


@router.post("/{campaign_id}/inject-npc-seed")
async def inject_npc_seed(campaign_id: str, req: GenerateRequest):
    """Generate an NPC and inject it as a pending seed in the active session."""
    if campaign_id not in _sessions:
        raise HTTPException(404, "No active session for this campaign")
    session = _sessions[campaign_id]
    world_ctx = _context_window_quietly(campaign_id)
    existing_names = []
    if session._npc_minds:
        existing_names = [m.name for m in session._npc_minds.get_all_minds(campaign_id)]
    npc = await _plot_generator.generate_npc(
        world_ctx, language=req.language, existing_npc_names=existing_names,
    )
    from dataclasses import asdict as _asdict
    npc_data = _asdict(npc)
    session._pending_npc_seed = npc_data
    session._pending_npc_introduced = False
    return {"status": "injected", "npc": npc_data}


@router.post("/{campaign_id}/timeskip")
async def timeskip(campaign_id: str, req: TimeskipRequest):
    _event_store.append(
        campaign_id=campaign_id,
        event_type=EventType.TIMESKIP,
        payload={"seconds": req.seconds},
        narrative_time_delta=req.seconds,
        location="world",
        entities=[],
    )
    world_ctx = _context_window_quietly(campaign_id)
    session_language = _sessions[campaign_id].language if campaign_id in _sessions else "en"
    world_changes = await _world_reactor.process_tick(
        campaign_id=campaign_id,
        narrative_seconds=req.seconds,
        world_context=world_ctx,
        language=session_language,
    )
    if world_changes:
        _event_store.append(
            campaign_id=campaign_id,
            event_type=EventType.WORLD_TICK,
            payload={"text": world_changes},
            narrative_time_delta=0,
            location="world",
            entities=[],
        )
        try:
            await _journal.evaluate_and_log(campaign_id, world_changes)
        except Exception:
            logger.warning("Failed to log timeskip world changes into journal", exc_info=True)
    return {"summary": world_changes or "Time passes quietly."}


@router.get("/{campaign_id}/inventory")
async def get_inventory(campaign_id: str):
    with GameStateStore(_game_state_db_path()) as store:
        state = store.get_state(campaign_id)
        if state is not None:
            world = store.get_world(state.world_id)
            if world is None:
                raise HTTPException(status_code=404, detail="Bound world not found")
            definitions = {item.id: item for item in world.items}
            return [
                {
                    "id": item_id,
                    "name": definitions[item_id].name if item_id in definitions else item_id,
                    "category": "misc",
                    "source": definitions[item_id].description if item_id in definitions else "",
                    "status": "carried",
                    "quantity": quantity,
                }
                for item_id, quantity in state.inventory.items()
                if quantity > 0
            ]
    _ensure_session(campaign_id)
    items = _inventory.get_inventory(campaign_id)
    return [{"name": i.name, "category": i.category, "source": i.source, "status": i.status} for i in items]


@router.post("/{campaign_id}/inventory")
async def update_inventory(campaign_id: str, req: InventoryActionRequest):
    with GameStateStore(_game_state_db_path()) as store:
        if store.get_state(campaign_id) is not None:
            raise HTTPException(
                status_code=409,
                detail="Bound campaigns must use the rules action endpoint to change inventory",
            )
    if req.action == "use":
        _inventory.use_item(campaign_id, req.name)
    elif req.action == "discard":
        _inventory.lose_item(campaign_id, req.name)
    return {"status": "ok"}


@router.get("/{campaign_id}/graph-search")
async def graph_search(campaign_id: str, q: str = ""):
    if not q:
        return {"facts": []}

    facts: list[dict] = []
    engine = _get_graphiti_engine()
    if engine:
        facts = await engine.search(campaign_id, q)

    if not facts:
        facts = await _fallback_graph_search(campaign_id, q)

    return {"facts": facts}


@router.get("/{campaign_id}/world-graph")
async def get_world_graph(campaign_id: str):
    try:
        engine = _get_graph_engine(campaign_id)
        if not engine:
            return {"nodes": [], "links": []}
        await engine.initialize()
        nodes = await engine.get_all_nodes()
        rels = await engine.get_all_relationships()
        return {
            "nodes": [
                {
                    "id": n.id,
                    "name": n.name,
                    "node_type": n.node_type.value,
                    "attributes": n.attributes,
                }
                for n in nodes
            ],
            "links": [
                {
                    "source": r["source_id"],
                    "target": r["target_id"],
                    "rel_type": r["rel_type"],
                    "strength": r["strength"],
                }
                for r in rels
            ],
        }
    except Exception as e:
        logger.warning("Failed to fetch world graph for campaign %s: %s", campaign_id, e)
        return {"nodes": [], "links": []}
