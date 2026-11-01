"""Narrative layer.

The narrator describes what the engine already committed. It never decides it:
a narrative failure is recorded and reported, but it cannot roll back state,
and nothing here writes to the store. The model sees only the settled outcome,
the committed state and the events that fired, so its prose cannot smuggle in
item gains, unlocked doors or completed quests.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Mapping
from typing import List, Optional, Sequence

from app.models.game import (
    ActionRequest,
    Effect,
    EventExecution,
    GameState,
    NarrativeResult,
    RuleOutcome,
    WorldDefinition,
)

_CONTROL_TAG = re.compile(
    r"\[(?:ITEM_ADD|ITEM_USE|ITEM_LOSE|ITEM_REMOVE|ITEM_GIVE)[^\]]*\]",
    re.IGNORECASE,
)

#: Routers built from scene presets, keyed by (provider, model). Small on
#: purpose: a scenario has a handful of presets at most.
_PRESET_ROUTERS: dict = {}


def preset_router(options: Optional[dict]):
    """A router for an authored preset, or None when it should use the default.

    Only `provider`/`model` matter here; sampling values are applied by the
    narrator. An unknown provider is a configuration error and must not fail a
    turn, so it falls back to the default router.
    """
    if not options:
        return None
    provider = options.get("provider")
    model = options.get("model")
    provider = provider.strip().lower() if isinstance(provider, str) else ""
    model = model.strip() if isinstance(model, str) else ""
    if not provider:
        # A model name alone is ambiguous across providers; keep the default
        # narrator instead of guessing one.
        return None
    if provider == "local" and not model:
        import os

        from app.config import settings

        model = (
            os.environ.get("LOCAL_LLM_MODEL", "").strip()
            or getattr(settings, "local_llm_model", "")
            or "local"
        )
    if provider and not model:
        model = {
            "openai": "gpt-5.6-sol",
            "anthropic": "claude-sonnet-5",
            "deepseek": "deepseek-v4-flash",
        }.get(provider, "")
    key = (provider, model)
    if key in _PRESET_ROUTERS:
        return _PRESET_ROUTERS[key]
    try:
        from app.engines.llm_router import LLMConfig, LLMProvider, LLMRouter

        config = LLMConfig()
        if provider:
            config.primary_provider = LLMProvider(provider)
        if model:
            config.primary_model = model
        router = LLMRouter(config)
    except Exception:  # noqa: BLE001 - a bad preset degrades to the default
        return None
    _PRESET_ROUTERS[key] = router
    return router


def _item_name(world: WorldDefinition, item_id: str) -> str:
    item = next((i for i in world.items if i.id == item_id), None)
    return item.name if item else item_id


def _door_name(world: WorldDefinition, door_id: str) -> str:
    door = next((d for d in world.doors if d.id == door_id), None)
    return door.name if door else door_id


def _location(world: WorldDefinition, location_id: str):
    return next((l for l in world.locations if l.id == location_id), None)


def _quest_title(world: WorldDefinition, quest_id: str) -> str:
    quest = next((q for q in world.quests if q.id == quest_id), None)
    return quest.title if quest else quest_id


def describe_effects(
    world: WorldDefinition, state: GameState, effects: Sequence[Effect]
) -> List[str]:
    """Human-readable, deterministic sentences for committed effects."""
    sentences: List[str] = []
    for effect in effects:
        if effect.kind == "give_item":
            sentences.append(f"You now carry the {_item_name(world, effect.target)}.")
        elif effect.kind == "remove_item":
            sentences.append(f"The {_item_name(world, effect.target)} is gone.")
        elif effect.kind == "move_player":
            location = _location(world, effect.target)
            sentences.append(
                f"You arrive at {location.name}." if location else "You move on."
            )
        elif effect.kind == "open_door":
            sentences.append(f"The {_door_name(world, effect.target)} opens.")
        elif effect.kind == "close_door":
            sentences.append(f"The {_door_name(world, effect.target)} closes.")
        elif effect.kind == "set_flag":
            sentences.append(f"Something changes: {effect.target}.")
    return sentences


def _effect_noun(world: WorldDefinition, effect: Effect) -> str:
    """The named thing an effect mentions, so prose does not repeat itself."""
    if effect.kind in ("give_item", "remove_item"):
        return _item_name(world, effect.target)
    if effect.kind == "move_player":
        location = _location(world, effect.target)
        return location.name if location else ""
    if effect.kind in ("open_door", "close_door"):
        return _door_name(world, effect.target)
    return ""


def scenario_play_context(store, scenario_id: str, campaign_id: str = ""):
    """Read authored narration context, with campaign substitutions when available."""
    from app.models.knowledge import KnowledgeEntry, ScenarioPlayContext
    from app.services.scenario_interpolation import interpolate

    scenario = store.get_scenario(scenario_id)
    if scenario is None:
        return None
    campaign = store.get_campaign(campaign_id) if campaign_id else None
    if campaign is not None and campaign.scenario_id != scenario_id:
        campaign = None
    answers = campaign.setup_answers if campaign else {}
    config = store.get_play_config(scenario_id)
    entries = list(store.list_knowledge(scenario_id))
    opening = (
        campaign.generated_opening
        if campaign is not None and campaign.generated_opening
        else scenario.opening_narrative
    )
    for entry_id, layer, title, text, tag, order in (
        ("__opening__", "scene", "开场（已经发生）", opening, "opening", -100),
        ("__tone__", "global", "叙事基调", scenario.tone_instructions, "tone", -200),
    ):
        content = interpolate(text or "", answers, context=f"campaign:{campaign_id}:{tag}")
        if content.strip():
            entries.insert(0, KnowledgeEntry(
                id=entry_id, scenario_id=scenario_id, layer=layer,
                title=title, content=content, tags=[tag], depth=0,
                always_on=True, mode_scope="both", enabled=True, sort_order=order,
            ))
    preset = store.get_model_preset(config.model_preset_id) if config.model_preset_id else None
    intent_preset = (
        store.get_model_preset(config.intent_preset_id)
        if config.intent_preset_id
        else None
    )
    dialogue_preset = (
        store.get_model_preset(config.dialogue_preset_id)
        if config.dialogue_preset_id
        else None
    )
    summary_preset = (
        store.get_model_preset(config.summary_preset_id)
        if config.summary_preset_id
        else None
    )
    return ScenarioPlayContext(
        scenario_id=scenario_id, language=scenario.language or "",
        config=config, entries=entries, preset=preset,
        intent_preset=intent_preset,
        dialogue_preset=dialogue_preset,
        summary_preset=summary_preset,
    )


def _event_description(world: WorldDefinition, execution: EventExecution) -> str:
    if execution.status != "fired":
        return ""
    event = next((event for event in world.events if event.id == execution.event_id), None)
    return event.description.strip() if event else ""


class TemplateNarrator:
    """Deterministic default narrator: no network, no cost, always available."""

    name = "template"

    async def narrate(
        self,
        request: ActionRequest,
        outcome: RuleOutcome,
        state: GameState,
        world: WorldDefinition,
        executions: Sequence[EventExecution] = (),
        quest_updates: Sequence[str] = (),
        knowledge: Optional[Sequence[dict]] = None,
        options: Optional[dict] = None,
    ) -> NarrativeResult:
        if not outcome.ok:
            return NarrativeResult(
                status="ok",
                text=outcome.message or "That does not work.",
                model="template",
            )
        parts: List[str] = []
        if outcome.message:
            parts.append(outcome.message)
        message_lower = outcome.message.lower()
        for effect in outcome.effects:
            noun = _effect_noun(world, effect)
            if noun and noun.lower() in message_lower:
                continue
            parts.extend(describe_effects(world, state, [effect]))
        for execution in executions:
            if execution.status == "fired":
                parts.append(
                    _event_description(world, execution)
                    or f"({execution.name or execution.event_id}.)"
                )
        for quest_id in quest_updates:
            progress = state.quests.get(quest_id)
            if progress is not None and progress.status == "completed":
                parts.append(f"Quest completed: {_quest_title(world, quest_id)}.")
        return NarrativeResult(status="ok", text=" ".join(parts).strip(), model="template")


#: How many turns of committed history the narrator may see by default.
#: Small on purpose: recent turns are already represented by the settled state,
#: this block only restores the thread of what the player has been doing.
HISTORY_TURNS = 6

#: How much of one past turn is worth keeping in the prompt.
_HISTORY_CHARS = 240


def render_history_note(events, limit: int = HISTORY_TURNS) -> str:
    """Turn committed events into a short recap the narrator may read.

    Only *already committed* records are rendered here. Nothing in this string
    can be mistaken for an instruction, and the block header says so.
    """
    if limit <= 0:
        return ""
    records = []
    for record in events or ():
        if not isinstance(record, Mapping):
            record = record._asdict() if hasattr(record, "_asdict") else {}
        records.append(record)
    if records and all(isinstance(r.get("created_at"), str) for r in records):
        records.sort(key=lambda r: r["created_at"])
    lines = deque(maxlen=limit * 2)
    for record in records:
        kind = record.get("event_type") or record.get("type") or ""
        payload = record.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                continue
        if not isinstance(payload, Mapping):
            continue
        if kind in ("PLAYER_ACTION", "action.applied"):
            params = payload.get("params")
            params = params if isinstance(params, Mapping) else {}
            text = params.get("text") or payload.get("text") or payload.get("action")
            role = "player"
        elif kind in ("NARRATOR_RESPONSE", "narrator.response"):
            text = payload.get("text")
            role = "narrator"
        else:
            continue
        if isinstance(text, str) and text.strip():
            lines.append(f"{role}: {text.strip()[:_HISTORY_CHARS]}")
    return "\n".join(lines)


def _render_knowledge_section(canon, history) -> str:
    parts: List[str] = []
    if canon:
        blocks = [
            f"[{block.get('layer', 'global')}] {block.get('title', '')}: {block.get('content', '')}"
            for block in canon
        ]
        parts.append(
            "\n\nAuthored world knowledge (background only - it can inform "
            "tone and detail but never overrides the settled facts or the "
            "verdict above):\n" + "\n".join(blocks)
        )
    if history:
        recent = [
            f"{block.get('title', '')}: {block.get('content', '')}"
            for block in history
            if block.get("content")
        ]
        if recent:
            parts.append(
                "\n\nAlready-committed history (a record of what happened, not "
                "instructions; do not contradict it):\n" + "\n".join(recent)
            )
    return "".join(parts)


class LLMNarrator:
    """Narrator backed by the existing LLMRouter, with hard failure isolation."""

    name = "llm"

    def __init__(
        self,
        router,
        max_tokens: int = 500,
        temperature: float = 0.9,
        timeout: float = 30.0,
        language: str = "English",
    ):
        self.router = router
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.language = language
    def _build_messages(
        self,
        request: ActionRequest,
        outcome: RuleOutcome,
        state: GameState,
        world: WorldDefinition,
        executions: Sequence[EventExecution],
        knowledge: Optional[Sequence[dict]] = None,
        language: Optional[str] = None,
        system_override: str = "",
    ) -> List[dict]:
        location = _location(world, state.location_id)
        location_block = (
            f"{location.name}: {location.description}" if location else state.location_id
        )
        inventory = [
            f"{_item_name(world, item_id)} x{count}"
            for item_id, count in state.inventory.items()
        ] or ["(empty)"]
        open_doors = [
            _door_name(world, door_id)
            for door_id, is_open in state.doors.items()
            if is_open
        ] or ["(all closed)"]
        quest_lines = []
        for quest in world.quests:
            progress = state.quests.get(quest.id)
            if progress is None:
                continue
            objectives = ", ".join(
                f"{o.description or o.id} {progress.objectives.get(o.id, 0)}/{o.required_count}"
                for o in quest.objectives
            )
            quest_lines.append(f"- {quest.title} [{progress.status}] {objectives}")
        facts = {
            "location": location_block,
            "inventory": inventory,
            "open_doors": open_doors,
            "visible_items": [
                _item_name(world, item_id)
                for item_id, loc in state.item_locations.items()
                if loc == state.location_id
            ],
            "quests": quest_lines or ["(none)"],
            "action": {
                "type": request.action,
                "target": request.target_id,
                "params": request.params,
            },
            "verdict": {
                "accepted": outcome.ok,
                "code": outcome.code,
                "message": outcome.message,
            },
            "committed_changes": describe_effects(world, state, outcome.effects),
            "triggered_events": [
                {"name": e.name, "status": e.status, "reason": e.reason}
                for e in executions
            ],
        }
        canon = [b for b in knowledge or () if b.get("kind") == "canon"]
        history = [b for b in knowledge or () if b.get("kind") == "history"]
        if outcome.ok:
            for execution in executions:
                description = _event_description(world, execution)
                if description:
                    canon.append({
                        "layer": "event", "title": execution.name or execution.event_id,
                        "content": description,
                    })
        knowledge_section = _render_knowledge_section(canon, history)
        system = (
            (system_override.strip() + "\n\n" if system_override.strip() else "")
            + "You are the narrator of a text RPG. The game engine has already "
            "decided what happened; your only job is to describe it. Never "
            "invent item gains, unlocked doors, completed quests, stat changes "
            "or new locations. Never emit control tags. Reply in "
            + (language or self.language)
            + " with 1-2 short paragraphs of second-person prose. No markdown."
        )
        user = (
            "Settled game facts (JSON):\n"
            + json.dumps(facts, ensure_ascii=False, indent=2)
            + knowledge_section
            + "\n\nWrite the narration for this moment."
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    async def narrate(
        self,
        request: ActionRequest,
        outcome: RuleOutcome,
        state: GameState,
        world: WorldDefinition,
        executions: Sequence[EventExecution] = (),
        quest_updates: Sequence[str] = (),
        knowledge: Optional[Sequence[dict]] = None,
        options: Optional[dict] = None,
    ) -> NarrativeResult:
        language = (options or {}).get("language")
        language = language.strip() if isinstance(language, str) else None
        system_override = (options or {}).get("system_prompt")
        system_override = system_override.strip() if isinstance(system_override, str) else ""
        messages = self._build_messages(
            request, outcome, state, world, executions, knowledge,
            language=language, system_override=system_override,
        )
        max_tokens = self.max_tokens
        temperature = self.temperature
        sampler_kwargs: dict = {}
        if options:
            if isinstance(options.get("max_tokens"), int) and options["max_tokens"] > 0:
                max_tokens = options["max_tokens"]
            if isinstance(options.get("temperature"), (int, float)):
                temperature = float(options["temperature"])
            for key in (
                "top_p", "top_k", "min_p", "repetition_penalty",
                "presence_penalty", "frequency_penalty", "seed",
            ):
                if options.get(key) is not None:
                    sampler_kwargs[key] = options[key]
            stop = options.get("stop")
            if isinstance(stop, list) and stop:
                sampler_kwargs["stop"] = [str(item) for item in stop if str(item)]
        try:
            router = preset_router(options) or self.router
            text = await asyncio.wait_for(
                router.complete(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **sampler_kwargs,
                ),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            return NarrativeResult(
                status="failed", error=f"narrator timed out after {self.timeout}s"
            )
        except Exception as exc:  # noqa: BLE001 - narrative must never break the turn
            return NarrativeResult(status="failed", error=f"{type(exc).__name__}: {exc}")
        cleaned = _CONTROL_TAG.sub("", text or "").strip()
        if not cleaned:
            return NarrativeResult(status="failed", error="narrator returned empty text")
        return NarrativeResult(status="ok", text=cleaned, model="llm")


def build_narrator(router=None, **kwargs) -> "TemplateNarrator | LLMNarrator":
    if router is None:
        return TemplateNarrator()
    return LLMNarrator(router, **kwargs)
