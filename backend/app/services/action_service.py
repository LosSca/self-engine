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
settled state, it cannot mutate it, and any exception it raises is converted
into NarrativeResult(status="failed"). The turn therefore still commits, the
failure is recorded as a narrative.failed domain event next to the change it
failed to describe, and a crash can never leave prose describing a change that
was not stored.
"""
from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db.game_state_store import GameStateStore, StateConflictError
from app.engines.event_engine import apply_effects, run_triggered_events
from app.engines.quest_engine import advance_quests
from app.engines.rules_engine import RulesEngine
from app.engines.story_engine import StoryStatus, advance_storyline
from app.models.game import (
    ActionResult,
    ActionRequest,
    Effect,
    GameState,
    NarrativeResult,
    RuleOutcome,
    WorldDefinition,
)
from app.models.knowledge import ScenarioPlayContext
from app.services.knowledge_service import build_context
from app.services.narrative_service import TemplateNarrator


def _now() -> str:
    return datetime.utcnow().isoformat()


class ActionService:
    """Deterministic, transactional action processing."""

    def __init__(
        self,
        store: GameStateStore,
        rules: Optional[RulesEngine] = None,
        narrator=None,
        context_provider=None,
        history_provider=None,
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
    def _load_play_context(self, world: WorldDefinition) -> Optional[ScenarioPlayContext]:
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
            return self.context_provider(scenario_id)
        except Exception:  # noqa: BLE001 - degrade instead of failing the action
            return None

    def _knowledge(
        self,
        play: Optional[ScenarioPlayContext],
        story_node: Optional[str],
        state: Optional[GameState] = None,
    ) -> tuple[List[Dict[str, Any]], List[str], Optional[Dict[str, Any]]]:
        provider = self.history_provider
        blocks: List[Dict[str, Any]] = []
        if play is None and (state is None or provider is None):
            # Nothing authored and no record to recall: no context at all.
            # Every local this function later assigns is already bound here, so
            # this early return cannot trip over an unbound name.
            return [], [], None
        if play is not None:
            try:
                context = build_context(
                    play.entries,
                    mode=play.config.mode,
                    max_depth=play.config.max_depth,
                    story_node=story_node,
                    storyline=play.config.storyline,
                    scenario_id=play.scenario_id,
                )
                blocks = [dict(block, kind="canon") for block in context.prompt_blocks()]
            except Exception:  # noqa: BLE001 - knowledge is advisory, never fatal
                blocks = []
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
            options = {
                "temperature": play.preset.temperature,
                "max_tokens": play.preset.max_tokens,
            }
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
        return result

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
                    self._load_play_context(world), None, state
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
        self.store.record_request(
            campaign_id=request.campaign_id,
            request_id=request.request_id,
            request_payload=payload,
            status="rejected",
            result_payload=result.model_dump(mode="json"),
            state_version=state.version,
        )
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
    ) -> tuple[NarrativeResult, List[str]]:
        """Describe a settled turn, never decide it.

        The state handed in here is already the committed result. Any failure
        of the narrator is reported in the returned NarrativeResult instead of
        propagating, so a flaky model cannot undo a rules-decided turn.
        """
        if not request.narrate:
            return NarrativeResult(status="skipped"), []
        knowledge: List[Dict[str, Any]] = []
        context_entries: List[str] = []
        options: Optional[Dict[str, Any]] = None
        try:
            blocks, context_entries, options = self._knowledge(
                play, active_node, new_state
            )
        except Exception as exc:  # noqa: BLE001 - context is advisory only
            return (
                NarrativeResult(status="failed", error=f"{type(exc).__name__}: {exc}"),
                [],
            )
        knowledge = blocks
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
        except Exception as exc:  # noqa: BLE001 - prose must never fail a turn
            return (
                NarrativeResult(
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                ),
                context_entries,
            )
        return narrative, context_entries

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
        applied = apply_effects(new_state, world, outcome.effects)
        failed = [item for item in applied if not item.ok]
        if failed:
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
                self.store.record_request(
                    campaign_id=request.campaign_id,
                    request_id=request.request_id,
                    request_payload=payload,
                    status="rejected",
                    result_payload=result.model_dump(mode="json"),
                    state_version=state.version,
                )
            return result, state, []

        facts = [fact for item in applied for fact in item.facts]
        combat_round = self._combat_round(request, state, new_state, applied)
        new_state, executions, event_facts = run_triggered_events(
            world, new_state, request.action, request.target_id, facts
        )
        new_state, quest_updates, _reward_facts = advance_quests(
            world, new_state, facts + event_facts
        )
        play = self._load_play_context(world)
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
            request, outcome, new_state, world, executions, quest_updates, play, active_node
        )

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
        for execution in executions:
            events.append(
                {
                    "event_type": f"event.{execution.status}",
                    "payload": {
                        "event_id": execution.event_id,
                        "name": execution.name,
                        "reason": execution.reason,
                        "effects": [e.model_dump(mode="json") for e in execution.effects],
                    },
                    "state_version": new_state.version,
                }
            )
        for update in quest_updates:
            quest_type = "quest.completed" if update.status == "completed" else "quest.progressed"
            events.append(
                {
                    "event_type": quest_type,
                    "payload": {
                        "quest_id": update.quest_id,
                        "objectives": update.objectives,
                    },
                    "state_version": new_state.version,
                }
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
        if narrative.status == "failed":
            events.append(
                {
                    "event_type": "narrative.failed",
                    "payload": {"error": narrative.error},
                    "state_version": new_state.version,
                }
            )
        return result, new_state, events

    # ------------------------------------------------------------- read side
    def get_state(self, campaign_id: str) -> Optional[GameState]:
        return self.store.get_state(campaign_id)

    def list_events(
        self, campaign_id: str, limit: int = 50, after_version: int = 0
    ) -> List[Dict[str, Any]]:
        return self.store.list_events(campaign_id, limit=limit, after_version=after_version)

    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get_request(request_id)