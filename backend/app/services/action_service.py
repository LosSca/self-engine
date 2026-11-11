"""Unified action pipeline: the one entry point that changes game state.

Order of operations for one request:
  1. validate the request payload (Pydantic, at the API boundary)
  2. idempotency: a known request_id is replayed, never re-applied
  3. load the authoritative state and its world
  4. rules decide the outcome (deterministic, no LLM, no mutation)
  5. rejected -> persist the request row only, state untouched
  6. accepted -> apply effects, fire events, advance quests on a copy
  7. narrate from that settled result (prose only; cannot change state)
  8. commit state + request row + domain events in ONE transaction, guarded by
     a compare-and-set on the version the turn was decided from

The narrator runs before the commit but has no write access: it receives the
settled state and cannot mutate it. If it raises, times out, or invents a reward
the rules did not award, the prose is replaced by a deterministic description of
what actually happened (NarrativeResult.model == "fallback") and the reason is
kept in .error. The turn therefore always commits, the failure is recorded as a
narrative.failed domain event next to the change it failed to describe, and a
crash can never leave prose describing a change that was not stored.
"""
from __future__ import annotations

import inspect
import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db.game_state_store import GameStateStore, StateConflictError
from app.engines.event_engine import (
    Fact,
    apply_effects,
    condition_holds,
    entity_location,
    run_triggered_events,
)
from app.engines.quest_engine import advance_quests
from app.engines.rules_engine import RulesEngine
from app.engines.story_engine import StoryStatus, advance_storyline
from app.models.game import (
    ActionResult,
    ActionRequest,
    Condition,
    Effect,
    GameState,
    NarrativeResult,
    RuleOutcome,
    WorldDefinition,
)
from app.models.knowledge import ScenarioPlayContext
from app.services.knowledge_service import build_context
from app.services.narrative_service import (
    TemplateNarrator,
    capture_diagnostics,
    preset_router,
    render_fallback,
)
from app.services.summary_service import SUMMARY_EVERY_TURNS, summarize


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.utcnow().isoformat()


#: Which authoritative fact an author-asserted call records from the world.
#: These are the same fact kinds events, quests and the rules engine speak.
FACT_KINDS = {
    "reach": "reach_location",
    "flag": "flag",
    "obtain": "item_gained",
    "open": "door_opened",
    "quest_progress": "quest_progress",
}


def _diagnostics_payload(narrative: NarrativeResult) -> Dict[str, Any]:
    """Describe the narrator call for a stored event, compactly.

    The block excerpts are kept: they are the only record of what the model
    was told, and without them a reviewer cannot explain a surprising line
    after the scenario has been edited. They are already truncated by
    `capture_diagnostics`, so the row stays small.
    """
    diagnostics = getattr(narrative, "diagnostics", None)
    if diagnostics is None:
        return {}
    payload = diagnostics.model_dump(mode="json")
    # An empty capture is not worth a row of nulls.
    if not diagnostics.blocks and not diagnostics.provider and not diagnostics.skipped:
        return {}
    return payload

def _settled_event_rows(
    executions, quest_updates, version: int
) -> List[Dict[str, Any]]:
    """Domain-event rows for what one settled batch triggered.

    Shared by the turn lane and the author/debug lane so both leave the same
    audit trail in the event store.
    """
    rows: List[Dict[str, Any]] = []
    for execution in executions:
        rows.append(
            {
                "event_type": f"event.{execution.status}",
                "payload": {
                    "event_id": execution.event_id,
                    "name": execution.name,
                    "reason": execution.reason,
                    "effects": [e.model_dump(mode="json") for e in execution.effects],
                    "branch": execution.branch,
                    "parent_event_id": execution.parent_event_id,
                    "depth": execution.depth,
                },
                "state_version": version,
            }
        )
    for update in quest_updates:
        if update.error:
            quest_type = "quest.reward_failed"
        elif update.status == "completed":
            quest_type = "quest.completed"
        elif update.status == "failed":
            quest_type = "quest.failed"
        else:
            quest_type = "quest.progressed"
        payload = {"quest_id": update.quest_id, "objectives": update.objectives}
        if update.error:
            payload["error"] = update.error
        if update.rewards_applied:
            payload["rewards"] = [
                effect.model_dump(mode="json") for effect in update.rewards_applied
            ]
        rows.append(
            {
                "event_type": quest_type,
                "payload": payload,
                "state_version": version,
            }
        )
    return rows


class ActionService:
    """Deterministic, transactional action processing."""

    def __init__(
        self,
        store: GameStateStore,
        rules: Optional[RulesEngine] = None,
        narrator=None,
        context_provider=None,
        history_provider=None,
        settled_sink=None,
    ):
        self.store = store
        self.rules = rules or RulesEngine()
        self.narrator = narrator or TemplateNarrator()
        #: Callable[[scenario_id], Optional[ScenarioPlayContext]] - authored
        #: knowledge, play mode and model preset for a world's scenario.
        self.context_provider = context_provider
        #: Callable[[campaign_id, GameState], Sequence[dict]] - read-only recap of
        #: what this campaign has already committed. Injected by the API layer so
        #: the domain service never reaches into the event store directly.
        self.history_provider = history_provider
        #: Callable[[str, GameState, str], None] - runs after a turn commits,
        #: outside the transaction. Only for work that may fail freely: the
        #: knowledge graph mirrors prose and must never roll a turn back.
        self.settled_sink = settled_sink

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _payload(request: ActionRequest) -> Dict[str, Any]:
        """Gameplay identity of the request, used for idempotency.

        Presentation flags such as `narrate` are deliberately excluded: asking
        for narration on a replay must not turn the replay into a conflict.
        """
        payload = request.model_dump(mode="json", exclude_none=True)
        payload.pop("narrate", None)
        return payload

    @staticmethod
    def _load_result(row: Dict[str, Any]) -> ActionResult:
        try:
            return ActionResult.model_validate(row["result"])
        except Exception:  # noqa: BLE001 - stored shape may predate a model change
            return ActionResult(
                request_id=row["request_id"],
                campaign_id=row["campaign_id"],
                status=row["status"],
                action=str(row.get("request", {}).get("action", "")),
                state_version=row.get("state_version", 0),
            )

    def _base(self, request: ActionRequest) -> Dict[str, Any]:
        return dict(
            request_id=request.request_id,
            campaign_id=request.campaign_id,
            action=request.action,
            actor_id=request.actor_id,
            target_id=request.target_id,
        )

    # -------------------------------------------------------------- context
    def _focus_tokens(
        self, world: WorldDefinition, state: GameState, request: ActionRequest
    ) -> List[str]:
        """Place, present NPCs and the target: who this turn can 'see'."""
        focus = [state.location_id] if state.location_id else []
        for entity in world.entities:
            if entity.kind == "object":
                continue
            if entity_location(state, world, entity.id) != state.location_id:
                continue
            if not condition_holds(
                state, world, Condition(kind="entity_alive", target=entity.id)
            ):
                continue
            focus.append(entity.id)
        if request.target_id:
            focus.append(request.target_id)
        seen: set[str] = set()
        ordered: List[str] = []
        for token in focus:
            key = token.strip().casefold()
            if key and key not in seen:
                seen.add(key)
                ordered.append(token)
        return ordered

    def _load_play_context(
        self, world: WorldDefinition, campaign_id: str = ""
    ) -> Optional[ScenarioPlayContext]:
        """Authored context for this world, or None when unavailable.

        Authored configuration must never be able to break a turn: a missing or
        broken context degrades to "no extra knowledge and no story nodes".
        """
        if self.context_provider is None:
            return None
        scenario_id = getattr(world, "scenario_id", "") or ""
        if not scenario_id:
            return None
        try:
            if campaign_id:
                try:
                    return self.context_provider(scenario_id, campaign_id)
                except TypeError:
                    pass
            return self.context_provider(scenario_id)
        except Exception:  # noqa: BLE001 - degrade instead of failing the action
            return None

    @staticmethod
    def _preset_sampling(preset) -> Dict[str, Any]:
        if preset is None:
            return {}
        values = {
            "temperature": preset.temperature,
            "max_tokens": preset.max_tokens,
        }
        for key in (
            "top_p", "top_k", "min_p", "repetition_penalty",
            "presence_penalty", "frequency_penalty", "seed",
        ):
            value = getattr(preset, key, None)
            if value is not None:
                values[key] = value
        if getattr(preset, "stop", None):
            values["stop"] = list(preset.stop)
        return values

    def _knowledge(
        self,
        play: Optional[ScenarioPlayContext],
        story_node: Optional[str],
        state: Optional[GameState] = None,
        story_nodes: Optional[List[str]] = None,
        focus: Optional[List[str]] = None,
        query: str = "",
        armed_packs: Optional[List[str]] = None,
    ) -> tuple[List[Dict[str, Any]], List[str], Optional[Dict[str, Any]]]:
        provider = self.history_provider
        blocks: List[Dict[str, Any]] = []
        if state is not None and state.summary:
            blocks.append(
                {
                    "id": f"longterm:{state.campaign_id}",
                    "layer": "history",
                    "title": "长期摘要",
                    "content": state.summary,
                    "kind": "history",
                }
            )
        if play is None and (state is None or provider is None):
            # Nothing authored and no record to recall: only long-term memory.
            return blocks, [block["id"] for block in blocks], None
        if play is not None:
            try:
                if play.config.mode == "storyline":
                    # Both accepted and refused narration use already-entered
                    # nodes, never a fresh progression evaluation. Branch and
                    # side knowledge stays relevant alongside the main line.
                    if story_nodes is None and state is not None:
                        story_nodes = [
                            node_id for node_id in (state.story_nodes or []) if node_id
                        ]
                else:
                    story_node = None
                    story_nodes = None
                context = build_context(
                    play.entries,
                    mode=play.config.mode,
                    max_depth=play.config.max_depth,
                    story_node=story_node,
                    storyline=play.config.storyline,
                    scenario_id=play.scenario_id,
                    story_nodes=story_nodes,
                    armed_packs=armed_packs,
                    focus=focus,
                    budget_chars=play.config.context_budget_chars,
                    query=query,
                )
                blocks.extend(
                    dict(block, kind="canon") for block in context.prompt_blocks()
                )
            except Exception:  # noqa: BLE001 - knowledge is advisory, never fatal
                # Keep the long-term summary already collected above; a broken
                # authored context must not erase memory from the prompt.
                pass
        #: Committed history is a separate channel from authored canon: it is
        #: written by the engine after each turn, never by the player or an editor,
        #: and the narrator is told it is a record rather than an instruction.
        if state is not None and provider is not None:
            try:
                history = provider(state.campaign_id, state) or []
            except Exception:  # noqa: BLE001 - a broken log must not fail a turn
                history = []
            for block in history:
                blocks.append(dict(block, kind="history"))
        options: Optional[Dict[str, Any]] = None
        if play is not None and play.preset is not None:
            options = self._preset_sampling(play.preset)
            if play.preset.role in ("narrator", "any"):
                if play.preset.provider:
                    options["provider"] = play.preset.provider
                if play.preset.model:
                    options["model"] = play.preset.model
        if play is not None and play.language:
            options = {**(options or {}), "language": play.language}
        prompt_parts = []
        if play is not None and play.preset is not None and play.preset.system_prompt:
            prompt_parts.append(play.preset.system_prompt)
        if play is not None and play.config.narrator_prompt:
            prompt_parts.append(play.config.narrator_prompt)
        if prompt_parts:
            options = {**(options or {}), "system_prompt": "\n\n".join(prompt_parts)}
        if not blocks:
            # Sampling settings are authored too: they apply even when the
            # scenario has no knowledge entries to hand over.
            return [], [], options
        return blocks, [block["id"] for block in blocks], options

    def _narrate_options(
        self, knowledge: List[Dict[str, Any]], options: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Pass authored knowledge only to narrators that accept it.

        Third-party or older narrators keep working unchanged: an unsupported
        keyword would raise TypeError mid-turn, which must never fail an action.
        """
        if not knowledge and options is None:
            return {}
        try:
            params = inspect.signature(self.narrator.narrate).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            return {}
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return {"knowledge": knowledge, "options": options}
        extra: Dict[str, Any] = {}
        if "knowledge" in params:
            extra["knowledge"] = knowledge
        if "options" in params:
            extra["options"] = options
        return extra

    # --------------------------------------------------------------- execute
    async def execute(self, request: ActionRequest) -> ActionResult:
        payload = self._payload(request)
        duplicate = self._replay(request, payload)
        if duplicate is not None:
            return duplicate

        state = self.store.get_state(request.campaign_id)
        if state is None:
            raise LookupError(
                f"campaign '{request.campaign_id}' has no game state; bind a world first"
            )
        world = self.store.get_world(state.world_id)
        if world is None:
            raise LookupError(
                f"world '{state.world_id}' for campaign '{request.campaign_id}' is missing"
            )

        outcome = self.rules.evaluate(
            world, state, request.action, request.target_id, request.params
        )
        if not outcome.ok:
            return await self._reject(request, payload, state, world, outcome)

        result, new_state, events = await self._apply(
            request, payload, state, world, outcome
        )
        if result.status != "applied":
            return result

        try:
            self.store.commit_action(
                campaign_id=request.campaign_id,
                request_id=request.request_id,
                request_payload=payload,
                status="applied",
                result_payload=result.model_dump(mode="json"),
                state=new_state,
                events=events,
                expected_version=state.version,
            )
        except sqlite3.IntegrityError:
            raced = self._replay(request, payload)
            if raced is None:
                raise
            return raced
        except StateConflictError as exc:
            # Another request committed first. Nothing of this turn was
            # written (the failed transaction also rolled back the request
            # row), so the same request_id can safely be decided again
            # from the new state; no side effect can be applied twice.
            retried = await self._retry_on_conflict(request, payload, attempts=1)
            if retried is not None:
                return retried
            return self._conflict(request, payload, exc)
        # The turn is durable now. Anything mirroring it elsewhere happens
        # after the fact and is not allowed to affect the answer.
        self._notify_settled(
            request.campaign_id, new_state, getattr(result, "narrative", None)
        )
        return result

    def _notify_settled(
        self, campaign_id: str, state: GameState, narrative: NarrativeResult
    ) -> None:
        """Hand a committed turn to external mirrors (knowledge graph, logs).

        This runs after the transaction, so a mirror that is down or slow can
        never roll a turn back; failures are reported to the sink itself.
        """
        if self.settled_sink is None:
            return
        try:
            self.settled_sink(campaign_id, state, narrative)
        except Exception:  # noqa: BLE001 - mirrors are best effort
            logger.warning("settled sink failed for %s", campaign_id, exc_info=True)

    async def _retry_on_conflict(
        self, request: ActionRequest, payload: Dict[str, Any], attempts: int
    ) -> Optional[ActionResult]:
        """Re-decide one write that lost a race, at most ``attempts`` times."""
        for _ in range(attempts):
            state = self.store.get_state(request.campaign_id)
            if state is None:
                return None
            world = self.store.get_world(state.world_id)
            if world is None:
                return None
            outcome = self.rules.evaluate(
                world, state, request.action, request.target_id, request.params
            )
            if not outcome.ok:
                return None
            quiet = request.model_copy(update={"narrate": False})
            result, new_state, events = await self._apply(
                quiet, payload, state, world, outcome, record_rejection=False
            )
            if result.status != "applied":
                return None
            try:
                self.store.commit_action(
                    campaign_id=request.campaign_id,
                    request_id=request.request_id,
                    request_payload=payload,
                    status="applied",
                    result_payload=result.model_dump(mode="json"),
                    state=new_state,
                    events=events,
                    expected_version=state.version,
                )
            except StateConflictError:
                continue
            except sqlite3.IntegrityError:
                return self._replay(request, payload)
            # The retried turn answers with the committed result and no prose:
            # narration is presentation only, and a second model call is not
            # worth blocking a database write on.
            result.message = (result.message + " (re-resolved after a concurrent change)").strip()
            return result
        return None

    def _conflict(
        self,
        request: ActionRequest,
        payload: Dict[str, Any],
        exc: Optional[StateConflictError],
    ) -> ActionResult:
        current = self.store.get_state(request.campaign_id)
        return ActionResult(
            **self._base(request),
            status="conflict",
            ok=False,
            code="version_conflict",
            message=(
                "the campaign state changed while this request was being resolved; "
                "retry with a new request_id"
            ),
            state_version=(
                current.version
                if current is not None
                else (exc.actual_version if exc is not None else 0)
            ),
            state=current,
        )

    def _replay(
        self, request: ActionRequest, payload: Dict[str, Any]
    ) -> Optional[ActionResult]:
        existing = self.store.get_request(request.request_id)
        if existing is None:
            return None
        current = self.store.get_state(request.campaign_id)
        version = current.version if current is not None else existing["state_version"]
        if existing["campaign_id"] != request.campaign_id or existing["request"] != payload:
            return ActionResult(
                **self._base(request),
                status="conflict",
                ok=False,
                code="request_id_conflict",
                message=(
                    f"request_id '{request.request_id}' was already used for a "
                    "different campaign or payload"
                ),
                state_version=version,
                state=current,
            )
        replay = self._load_result(existing)
        replay.status = "duplicate"
        replay.message = "duplicate request ignored; no side effects were applied"
        replay.state_version = version
        if current is not None:
            replay.state = current
        return replay

    async def _reject(
        self,
        request: ActionRequest,
        payload: Dict[str, Any],
        state: GameState,
        world: WorldDefinition,
        outcome: RuleOutcome,
    ) -> ActionResult:
        # A refused action also gets prose, but only as explanation of the
        # refusal: the state it describes is untouched either way.
        knowledge: List[Dict[str, Any]] = []
        context_entries: List[str] = []
        options: Optional[Dict[str, Any]] = None
        narrative = NarrativeResult(status="skipped")
        if request.narrate:
            try:
                blocks, context_entries, options = self._knowledge(
                    self._load_play_context(world, state.campaign_id),
                    None,
                    state,
                    focus=self._focus_tokens(world, state, request),
                    query=str(request.params.get("text") or ""),
                )
                knowledge = blocks
                narrative = await self.narrator.narrate(
                    request,
                    outcome,
                    state,
                    world,
                    **self._narrate_options(knowledge, options),
                )
            except Exception as exc:  # noqa: BLE001 - prose must never fail a turn
                narrative = NarrativeResult(
                    status="failed", error=f"{type(exc).__name__}: {exc}"
                )
        if narrative.status != "ok":
            # A refused action still gets a sentence, and a broken model must
            # not turn that into an error: the refusal itself is the answer.
            narrative = NarrativeResult(
                status="ok",
                text=outcome.message or outcome.code,
                model="fallback",
                error=narrative.error,
            )
        result = ActionResult(
            **self._base(request),
            status="rejected",
            ok=False,
            code=outcome.code,
            message=outcome.message,
            state_version=state.version,
            context_entries=context_entries,
            narrative=narrative,
            state=state,
        )
        try:
            self.store.record_request(
                campaign_id=request.campaign_id,
                request_id=request.request_id,
                request_payload=payload,
                status="rejected",
                result_payload=result.model_dump(mode="json"),
                state_version=state.version,
            )
        except sqlite3.IntegrityError:
            raced = self._replay(request, payload)
            if raced is None:
                raise
            return raced
        return result

    @staticmethod
    def _combat_round(
        request: ActionRequest,
        state: GameState,
        new_state: GameState,
        applied,
    ) -> Optional[Dict[str, Any]]:
        """Describe the deterministic combat turn this request resolved.

        The round record is built from settled state, so it can be stored in
        the same transaction as the change it describes and replayed verbatim
        on a duplicate request.
        """
        if request.action not in ("attack", "defend", "flee"):
            return None
        target_id = request.target_id or str(state.combat.get("target_id") or "")
        before = state.combatants.get(target_id)
        after = new_state.combatants.get(target_id)
        return {
            "round": int(new_state.flags.get("combat_round", 0) or 0),
            "action": request.action,
            "target_id": target_id,
            "target_hp_before": before.hp if before else None,
            "target_hp_after": after.hp if after else None,
            "target_alive": after.alive if after else None,
            "player_hp_before": state.flags.get("player_hp"),
            "player_hp_after": new_state.flags.get("player_hp"),
            "player_defeated": bool(new_state.flags.get("player_defeated")),
            "in_combat": bool(new_state.combat.get("active")),
            "facts": [fact.kind for item in applied for fact in item.facts],
        }

    async def _call_narrator(self, *args, **kwargs):
        return await self.narrator.narrate(*args, **kwargs)

    def _append_summary(
        self,
        state: GameState,
        request: ActionRequest,
        outcome: RuleOutcome,
        narrative: NarrativeResult,
    ) -> None:
        """Keep a bounded, deterministic recap of committed turns."""
        text = (narrative.text or "").strip() or (outcome.message or "").strip()
        if not text:
            return
        line = f"[T{state.turn}] {text[:200]}"
        lines = [item for item in (state.summary or "").split("\n") if item]
        lines.append(line)
        state.summary = "\n".join(lines[-12:])[-2000:]

    async def _maybe_llm_summary(self, state: GameState, play) -> None:
        if play is None or play.summary_preset is None:
            return
        if play.summary_preset.role not in ("summary", "any"):
            return
        if state.turn % SUMMARY_EVERY_TURNS != 0:
            return
        router = preset_router(
            {
                "provider": play.summary_preset.provider,
                "model": play.summary_preset.model,
            }
        )
        if router is None:
            return
        summarized = await summarize(router, state.summary)
        if summarized:
            state.summary = summarized

    async def _narrate_applied(
        self,
        request: ActionRequest,
        outcome: RuleOutcome,
        new_state: GameState,
        world: WorldDefinition,
        executions: List[Any],
        quest_updates: List[Any],
        play: Optional[ScenarioPlayContext],
        active_node: Optional[str],
        armed_packs: Optional[List[str]] = None,
    ) -> tuple[NarrativeResult, List[str]]:
        """Describe a settled turn, never decide it.

        The state handed in here is already the committed result. Any failure
        of the narrator is reported in the returned NarrativeResult instead of
        propagating, so a flaky model cannot undo a rules-decided turn.
        """
        if not request.narrate:
            # Record that the caller asked for no prose, so the debug panel can
            # tell "narrate=false" apart from "the narrator was configured",
            # while the capture itself still costs nothing.
            cap = capture_diagnostics(None, None, skipped=True)
            return NarrativeResult(
                status="skipped", diagnostics=cap
            ), []
        knowledge: List[Dict[str, Any]] = []
        context_entries: List[str] = []
        options: Optional[Dict[str, Any]] = None
        try:
            # Every entered node contributes its knowledge, plus the event packs
            # currently armed for this point of the story.
            blocks, context_entries, options = self._knowledge(
                play,
                active_node,
                new_state,
                story_nodes=[n for n in (new_state.story_nodes or []) if n],
                focus=self._focus_tokens(world, new_state, request),
                query=str(request.params.get("text") or ""),
                armed_packs=armed_packs or [],
            )
        except Exception as exc:  # noqa: BLE001 - context is advisory only
            return (
                NarrativeResult(status="failed", error=f"{type(exc).__name__}: {exc}"),
                [],
            )
        knowledge = blocks
        if (
            play is not None
            and request.action == "talk"
            and play.dialogue_preset is not None
            and play.dialogue_preset.role in ("dialogue", "any")
            and play.dialogue_preset.provider
        ):
            options = {
                **(options or {}),
                **self._preset_sampling(play.dialogue_preset),
                "provider": play.dialogue_preset.provider,
                "model": play.dialogue_preset.model,
            }
            if play.dialogue_preset.system_prompt:
                options["system_prompt"] = "\n\n".join(
                    filter(
                        None,
                        [
                            play.dialogue_preset.system_prompt,
                            play.config.narrator_prompt,
                        ],
                    )
                )
        event_effects = [
            effect for execution in executions for effect in getattr(execution, "effects", ())
        ]
        try:
            narrative = await self.narrator.narrate(
                request,
                outcome,
                new_state,
                world,
                executions,
                [update.quest_id for update in quest_updates],
                **self._narrate_options(knowledge, options),
                executions_effects=event_effects,
            )
        except TypeError as exc:
            # A narrator written before event effects were accounted for still
            # gets the call, without the newer keyword.
            if "executions_effects" not in str(exc):
                return (
                    NarrativeResult(status="failed", error=f"{type(exc).__name__}: {exc}"),
                    context_entries,
                )
            try:
                narrative = await self.narrator.narrate(
                    request,
                    outcome,
                    new_state,
                    world,
                    executions,
                    [update.quest_id for update in quest_updates],
                    **self._narrate_options(knowledge, options),
                )
            except Exception as inner:  # noqa: BLE001
                return (
                    NarrativeResult(status="failed", error=f"{type(inner).__name__}: {inner}"),
                    context_entries,
                )
        except Exception as exc:  # noqa: BLE001 - prose must never fail a turn
            narrative = NarrativeResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        if narrative.status != "ok":
            # The model is unavailable or its answer was rejected: describe the
            # committed result deterministically instead of leaving the player
            # with an error line. The failure itself is still reported.
            # A failed narrator never reads as an error to the player: the
            # engine can describe its own committed result.
            fallback = render_fallback(world, new_state, outcome, executions)
            if fallback:
                return (
                    NarrativeResult(
                        status="ok",
                        text=fallback,
                        model="fallback",
                        error=narrative.error,
                        # The model call did happen; keeping its capture is
                        # what lets a reviewer see why it was rejected.
                        diagnostics=narrative.diagnostics,
                    ),
                    context_entries,
                )
        return narrative, context_entries

    def _reject_effects(
        self,
        request: ActionRequest,
        payload: Dict[str, Any],
        state: GameState,
        failed,
        record_rejection: bool,
    ) -> tuple[ActionResult, GameState, List[Dict[str, Any]]]:
        result = ActionResult(
            **self._base(request),
            status="rejected",
            ok=False,
            code="effect_failed",
            message=failed[0].error,
            state_version=state.version,
            state=state,
        )
        if record_rejection:
            try:
                self.store.record_request(
                    campaign_id=request.campaign_id,
                    request_id=request.request_id,
                    request_payload=payload,
                    status="rejected",
                    result_payload=result.model_dump(mode="json"),
                    state_version=state.version,
                )
            except sqlite3.IntegrityError:
                raced = self._replay(request, payload)
                if raced is None:
                    raise
                return raced, state, []
        return result, state, []

    def _settle_facts(
        self,
        world: WorldDefinition,
        new_state: GameState,
        request: ActionRequest,
        facts: List[Fact],
        processed: set[str],
        forced: set[str],
        origins: dict,
        attempted_quests: set[str],
    ):
        """Run events and quests for one settled movement/action step."""
        executions = []
        quest_updates = []
        new_state, first_executions, event_facts = run_triggered_events(
            world, new_state, request.action, request.target_id, facts,
            processed=processed, forced=forced, origins=origins,
        )
        executions.extend(first_executions)
        new_state, updates, reward_facts = advance_quests(
            world, new_state, facts + event_facts, attempted=attempted_quests
        )
        quest_updates.extend(updates)
        for _ in range(len(world.events) + len(world.quests) + 1):
            if not reward_facts:
                break
            new_state, more_executions, event_facts = run_triggered_events(
                world, new_state, request.action, request.target_id,
                reward_facts, processed=processed, forced=forced, origins=origins,
            )
            executions.extend(more_executions)
            if not event_facts:
                break
            # Reward facts were consumed inside advance_quests; only events are new.
            new_state, more_updates, reward_facts = advance_quests(
                world, new_state, event_facts, attempted=attempted_quests
            )
            quest_updates.extend(more_updates)
        return new_state, executions, quest_updates

    async def _apply(
        self,
        request: ActionRequest,
        payload: Dict[str, Any],
        state: GameState,
        world: WorldDefinition,
        outcome: RuleOutcome,
        record_rejection: bool = True,
    ) -> tuple[ActionResult, GameState, List[Dict[str, Any]]]:
        new_state = state.model_copy(deep=True)
        processed: set[str] = set()
        forced: set[str] = set()
        origins: dict = {}
        attempted_quests: set[str] = set()
        executions = []
        quest_updates = []

        raw_path = (
            request.params.get("path") if isinstance(request.params, dict) else None
        )
        multi_path = (
            request.action == "move"
            and isinstance(raw_path, list)
            and len(raw_path) > 1
        )
        if multi_path:
            applied = []
            stopped = ""
            for step in raw_path:
                segment = self.rules.evaluate(world, new_state, "move", step)
                if not segment.ok:
                    if not applied:
                        refused = await self._reject(
                            request, payload, state, world, segment
                        )
                        return refused, state, []
                    stopped = segment.message
                    break
                segment_applied = apply_effects(new_state, world, segment.effects)
                failed = [item for item in segment_applied if not item.ok]
                if failed:
                    return self._reject_effects(
                        request, payload, state, failed, record_rejection
                    )
                applied.extend(segment_applied)
                step_facts = [
                    fact for item in segment_applied for fact in item.facts
                ]
                new_state, step_executions, step_updates = self._settle_facts(
                    world, new_state, request, step_facts,
                    processed, forced, origins, attempted_quests,
                )
                executions.extend(step_executions)
                quest_updates.extend(step_updates)
                if (
                    new_state.combat.get("active")
                    or new_state.flags.get("combat_active")
                    or new_state.flags.get("player_defeated")
                ):
                    stopped = "Combat interrupts the journey."
                    break
            if not applied:  # pragma: no cover - the full path was validated above
                refused = await self._reject(request, payload, state, world, outcome)
                return refused, state, []
            if stopped:
                here = next(
                    (loc for loc in world.locations if loc.id == new_state.location_id),
                    None,
                )
                message = f"You stop at {here.name}. {stopped}" if here else stopped
            else:
                here = next(
                    (loc for loc in world.locations if loc.id == new_state.location_id),
                    None,
                )
                message = (
                    f"You end up at {here.name}."
                    if here is not None and here.id != request.target_id
                    else outcome.message
                )
            outcome = outcome.model_copy(
                update={
                    "effects": [item.effect for item in applied],
                    "message": message,
                }
            )
        else:
            applied = apply_effects(new_state, world, outcome.effects)
            failed = [item for item in applied if not item.ok]
            if failed:
                return self._reject_effects(
                    request, payload, state, failed, record_rejection
                )
            facts = [fact for item in applied for fact in item.facts]
            new_state, executions, quest_updates = self._settle_facts(
                world, new_state, request, facts,
                processed, forced, origins, attempted_quests,
            )

        combat_round = self._combat_round(request, state, new_state, applied)
        play = self._load_play_context(world, state.campaign_id)
        story_status: Optional[StoryStatus] = None
        story_entered: List[str] = []
        active_node: Optional[str] = None
        story_error = ""
        if play is not None and play.config.mode == "storyline":
            try:
                story_status = advance_storyline(play.config.storyline, new_state, world)
                story_entered = list(story_status.newly_entered)
                active_node = story_status.active
            except Exception as exc:  # noqa: BLE001 - story data is authored input
                story_error = f"{type(exc).__name__}: {exc}"
        new_state.version = state.version + 1
        new_state.turn = state.turn + 1
        new_state.updated_at = _now()

        # Narration is presentation only: it reads the settled state but can
        # never fail, roll back or rewrite it. A model timeout or a bad payload
        # degrades to status="failed" and the turn still commits.
        narrative, context_entries = await self._narrate_applied(
            request,
            outcome,
            new_state,
            world,
            executions,
            quest_updates,
            play,
            active_node,
            list(story_status.armed_packs) if story_status is not None else [],
        )
        self._append_summary(new_state, request, outcome, narrative)
        await self._maybe_llm_summary(new_state, play)

        result = ActionResult(
            **self._base(request),
            status="applied",
            ok=True,
            code=outcome.code,
            message=outcome.message,
            state_version=new_state.version,
            effects_applied=outcome.effects,
            events_fired=executions,
            quests={qid: progress.status for qid, progress in new_state.quests.items()},
            story_nodes_entered=story_entered,
            context_entries=context_entries,
            combat_round=combat_round,
            narrative=narrative,
            state=new_state,
        )
        events: List[Dict[str, Any]] = [
            {
                "event_type": "action.applied",
                "payload": {
                    "action": request.action,
                    "target": request.target_id,
                    "code": outcome.code,
                    "effects": [e.model_dump(mode="json") for e in outcome.effects],
                },
                "state_version": new_state.version,
            }
        ]
        events.extend(
            _settled_event_rows(executions, quest_updates, new_state.version)
        )
        if story_status is not None:
            node_map = play.config.storyline.node_map() if play is not None else {}
            for node_id in story_entered:
                node = node_map.get(node_id)
                events.append(
                    {
                        "event_type": "story.entered",
                        "payload": {
                            "node_id": node_id,
                            "title": node.title if node else "",
                            "kind": node.kind if node else "",
                        },
                        "state_version": new_state.version,
                    }
                )
        if story_error:
            events.append(
                {
                    "event_type": "story.error",
                    "payload": {"error": story_error},
                    "state_version": new_state.version,
                }
            )
        if narrative.error:
            # A degraded turn keeps its prose (a deterministic description of
            # the committed change) but never hides why the model was skipped:
            # the reason is the only thing a debugger can act on.
            events.append(
                {
                    "event_type": "narrative.failed",
                    "payload": {
                        "error": narrative.error,
                        "degraded": narrative.status == "ok",
                        "model": narrative.model,
                    },
                    "state_version": new_state.version,
                }
            )
        if narrative.status == "ok" and narrative.text:
            events.append(
                {
                    "event_type": "narrator.response",
                    "payload": {
                        "text": narrative.text,
                        "model": getattr(narrative, "model", ""),
                        "diagnostics": _diagnostics_payload(narrative),
                    },
                    "state_version": new_state.version,
                }
            )
        return result, new_state, events


    async def record_quest_progress(
        self,
        campaign_id: str,
        action: str,
        target_id: str,
        request_id: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> ActionResult:
        """Commit facts an author asserted, through the normal write path.

        Debug tooling (a manual quest nudge, an author's simulation being
        promoted) needs to move the world forward without pretending a player
        performed a rules-checked action. It therefore skips the rule engine
        but keeps everything that makes the write trustworthy: the facts are
        ordinary validated facts, they settle through the same event/quest
        pipeline as a turn, the commit is one transaction against an expected
        version, the request row keeps it idempotent, and the narrated answer
        is generated afterwards from the committed state only.
        """
        fact_kind = FACT_KINDS.get(action)
        if fact_kind is None:
            raise ValueError(f"unsupported fact action '{action}'")
        amount = 1
        raw_amount = (params or {}).get("amount")
        if raw_amount is not None:
            try:
                amount = int(raw_amount)
            except (TypeError, ValueError):
                raise ValueError("amount must be an integer") from None
        if isinstance(raw_amount, bool):
            raise ValueError("amount must be an integer")
        if amount < 1 or amount > 100:
            raise ValueError("amount must be between 1 and 100")

        request = ActionRequest(
            campaign_id=campaign_id,
            request_id=request_id,
            # The request carries the fact being asserted, not a player verb:
            # this path cannot pretend a rules-checked action happened.
            action="custom",
            target_id=target_id,
            params={**dict(params or {}), "kind": action},
            narrate=False,
        )
        payload = self._payload(request)
        duplicate = self._replay(request, payload)
        if duplicate is not None:
            return duplicate

        state = self.store.get_state(campaign_id)
        if state is None:
            raise LookupError(
                f"campaign '{campaign_id}' has no game state; bind a world first"
            )
        world = self.store.get_world(state.world_id)
        if world is None:
            raise LookupError(
                f"world '{state.world_id}' for campaign '{campaign_id}' is missing"
            )
        new_state = state.model_copy(deep=True)
        if fact_kind == "quest_progress":
            # An objective is not advanced by a fact in this engine: the
            # `quest_progress` effect is what moves the counter (it is how a
            # configured event moves it too), so the tool reuses that effect
            # and only feeds the resulting fact into the quest/event lane.
            quest_id, _, objective_id = target_id.partition(":")
            progress_effect = Effect(
                kind="quest_progress", target=quest_id, value=objective_id, amount=amount
            )
            applied = apply_effects(new_state, world, [progress_effect])
            if not applied[0].ok:
                refused, _, _ = self._reject_effects(
                    request, payload, state, applied, record_rejection=True
                )
                return refused
            facts = [fact for item in applied for fact in item.facts]
        else:
            facts = [Fact(fact_kind, target_id)]
        # An authored fact is not a player attempt: events armed on the custom
        # verb fire, so a designer can hang a hook on "something was asserted"
        # without inventing a fake player action.
        outcome = RuleOutcome(
            ok=True,
            code="author_asserted",
            message=f"author asserted {action} {target_id}",
            effects=[],
            facts=[],
        )

        new_state, executions, quest_updates = self._settle_facts(
            world, new_state, request, facts,
            set(), set(), {}, set(),
        )
        new_state.version = state.version + 1
        new_state.turn = state.turn + 1
        new_state.updated_at = _now()

        result = ActionResult(
            **self._base(request),
            status="applied",
            ok=True,
            code=outcome.code,
            message=outcome.message,
            state_version=new_state.version,
            effects_applied=[],
            events_fired=executions,
            quests={qid: progress.status for qid, progress in new_state.quests.items()},
            state=new_state,
        )
        events: List[Dict[str, Any]] = [
            {
                "event_type": "debug.asserted",
                "payload": {
                    "action": action,
                    "target": target_id,
                    "amount": amount,
                    "fact": fact_kind,
                },
                "state_version": new_state.version,
            }
        ]
        events.extend(_settled_event_rows(executions, quest_updates, new_state.version))
        try:
            self.store.commit_action(
                campaign_id=campaign_id,
                request_id=request_id,
                request_payload=payload,
                status="applied",
                result_payload=result.model_dump(mode="json"),
                state=new_state,
                events=events,
                expected_version=state.version,
            )
        except sqlite3.IntegrityError:
            raced = self._replay(request, payload)
            if raced is None:
                raise
            return raced
        except StateConflictError as exc:
            return self._conflict(request, payload, exc)
        self._notify_settled(campaign_id, new_state, None)
        return result

    # ------------------------------------------------------------- read side
    def get_state(self, campaign_id: str) -> Optional[GameState]:
        return self.store.get_state(campaign_id)

    def list_events(
        self, campaign_id: str, limit: int = 50, after_version: int = 0
    ) -> List[Dict[str, Any]]:
        return self.store.list_events(campaign_id, limit=limit, after_version=after_version)

    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get_request(request_id)