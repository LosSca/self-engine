"""One action pipeline for typed text, map clicks and the legacy chat surface.

The parser is deterministic and offline: it turns a short player utterance into
the same ActionRequest the structured API accepts. Only when the text cannot be
mapped at all may an optional intent advisor (an LLM) suggest a *candidate*
action, and even then the suggestion is re-validated against the same whitelist
and replayed through the rule engine - the model never writes state.

Bound campaigns are resolved against a world whose scenario_id matches the
campaign's scenario, so a scenario created in the UI keeps working without the
player having to bind a world manually.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.engines.action_registry import action_names, verb_table
from app.models.game import ActionRequest, ActionResult, GameState, WorldDefinition
from app.services import world_service
from app.services.action_service import ActionService

#: The parser accepts exactly the actions the rule catalogue declares, and the
#: verb table is read from the same place, so the two can never drift apart.
ACTIONS = action_names()

#: Verb table. Longest phrases first so "打开" wins over "打".
_VERBS: Sequence[Tuple[str, Tuple[str, ...]]] = verb_table()

_TRAILING = ("", "。", "！", "?", "？", ".", "吧", "啊", "呢")
_LEADING_PARTICLES = ("请", "我想", "我要", "帮我", "试着", "试图", "麻烦")


@dataclass
class ParsedAction:
    action: str
    target_id: Optional[str] = None
    matched_name: str = ""
    confidence: str = "exact"
    candidates: List[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target_id": self.target_id,
            "matched_name": self.matched_name,
            "confidence": self.confidence,
            "candidates": list(self.candidates),
            "note": self.note,
        }


def _norm(text: str) -> str:
    value = (text or "").strip()
    for prefix in _LEADING_PARTICLES:
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    return value


def _clean_target(text: str) -> str:
    value = text.strip().strip("，,、:：")
    for suffix in ("这个", "那个"):
        if value.startswith(suffix):
            value = value[len(suffix):]
    return value.strip()


def _aliases(names: Sequence[str]) -> List[str]:
    """Normalised lookup names, including underscore/spaced spellings.

    Authors write ids as iron_key; players type "iron key". Both spellings
    should resolve to the same candidate, so every name also contributes a
    space-joined and an underscore-joined variant.
    """
    out: List[str] = []
    seen = set()
    for name in names:
        if not name:
            continue
        base = name.strip().lower()
        if not base:
            continue
        for variant in (base, base.replace("_", " "), base.replace(" ", "_")):
            cleaned = " ".join(variant.split())
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
    return out


class _Candidate:
    __slots__ = ("id", "names", "kind")

    def __init__(self, item_id: str, names: Sequence[str], kind: str):
        self.id = item_id
        self.names = _aliases(names)
        self.kind = kind

    def match(self, fragment: str) -> int:
        fragment = fragment.strip().lower().replace("_", " ")
        if not fragment:
            return 0
        best = 0
        for name in self.names:
            if name == fragment:
                return 1000 + len(name)
            if fragment.startswith(name) or name.startswith(fragment):
                best = max(best, 500 + min(len(name), len(fragment)))
            elif name in fragment:
                best = max(best, 200 + len(name))
        return best


class TextActionParser:
    """Deterministic utterance -> (action, target) mapping."""

    def parse(
        self, text: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        raw = _norm(text)
        if not raw:
            return None
        lowered = raw.lower()
        for suffix in _TRAILING:
            if suffix and lowered.endswith(suffix):
                lowered = lowered[: -len(suffix)].strip()
        action, fragment = self._split_verb(lowered)
        if action is None:
            guessed = self._guess_without_verb(lowered, world, state)
            if guessed is None:
                return None
            action = guessed.action
            fragment = guessed.matched_name
            parsed = self._resolve(action, fragment, world, state)
            if parsed is None:
                return guessed
            return parsed
        parsed = self._resolve(action, fragment, world, state)
        if parsed is not None:
            return parsed
        return ParsedAction(
            action=action,
            confidence="missing_target",
            note=f"could not resolve a target from '{fragment}'",
        )

    def _split_verb(self, text: str) -> Tuple[Optional[str], str]:
        for action, verbs in _VERBS:
            for verb in verbs:
                if text == verb:
                    return action, ""
                if text.startswith(verb):
                    return action, _clean_target(text[len(verb):])
                if verb in ("look", "go", "take") and text.startswith(verb + " "):
                    return action, text[len(verb):].strip()
        return None, text

    def _guess_without_verb(
        self, text: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        candidates: List[Tuple[str, _Candidate]] = []
        candidates.extend(("move", c) for c in self._move_candidates(world, state))
        candidates.extend(("take", c) for c in self._item_candidates(world, state))
        candidates.extend(("talk", c) for c in self._talk_candidates(world, state))
        candidates.extend(("open", c) for c in self._door_candidates(world, state))
        best: Optional[Tuple[int, str, _Candidate]] = None
        for action, candidate in candidates:
            if candidate.match(text) == 1000 + len(text):
                return ParsedAction(
                    action=action,
                    target_id=candidate.id,
                    matched_name=candidate.names[0] if candidate.names else candidate.id,
                    confidence="name_only",
                )
            score = candidate.match(text)
            if score and (best is None or score > best[0]):
                best = (score, action, candidate)
        if best is None:
            return None
        return ParsedAction(
            action=best[1],
            target_id=best[2].id,
            matched_name=best[2].names[0] if best[2].names else best[2].id,
            confidence="name_only",
        )

    def _resolve(
        self, action: str, fragment: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        if action == "look":
            return ParsedAction(action="look", confidence="verb_only")
        if action == "defend":
            enemies = self._enemy_candidates(world, state)
            matched = self._best(fragment, enemies) if fragment else None
            return ParsedAction(
                action="defend",
                target_id=matched.id if matched else None,
                matched_name=self._display(matched, world, "defend") if matched else "",
                confidence="verb_only" if matched is None else "exact",
            )
        if action == "flee":
            return ParsedAction(action="flee", confidence="verb_only")
        if action == "attack":
            enemies = self._enemy_candidates(world, state)
            matched = self._best(fragment, enemies) if fragment else None
            if matched is None and not enemies:
                return ParsedAction(
                    action="attack",
                    confidence="no_target",
                    note="没有可以攻击的目标。",
                )
            if matched is None:
                # One hostile present is an unambiguous target.
                matched = enemies[0] if len(enemies) == 1 else None
            return ParsedAction(
                action="attack",
                target_id=matched.id if matched else None,
                matched_name=self._display(matched, world, "attack") if matched else "",
                confidence="exact" if matched else "ambiguous",
            )
        if action == "move":
            rules = ("move", self._move_candidates(world, state))
        elif action in ("take", "use"):
            rules = (action, self._item_candidates(world, state))
        elif action == "open":
            rules = ("open", self._door_candidates(world, state))
        elif action == "talk":
            rules = ("talk", self._talk_candidates(world, state))
        else:
            return None
        matched = self._best(fragment, rules[1])
        if matched is None and fragment:
            # A bare word may be the target's display name without a verb.
            for other_action, candidates in (
                ("move", self._move_candidates(world, state)),
                ("take", self._item_candidates(world, state)),
                ("open", self._door_candidates(world, state)),
                ("talk", self._talk_candidates(world, state)),
            ):
                exact = self._best(fragment, candidates, exact_only=True)
                if exact is not None:
                    return ParsedAction(
                        action=other_action,
                        target_id=exact.id,
                        matched_name=self._display(exact, world, other_action),
                        confidence="exact",
                    )
        if matched is None:
            return None
        return ParsedAction(
            action=rules[0],
            target_id=matched.id,
            matched_name=self._display(matched, world, rules[0]),
            confidence="exact",
        )

    @staticmethod
    def _best(
        fragment: str, candidates: List[_Candidate], exact_only: bool = False
    ) -> Optional[_Candidate]:
        ranked = sorted(
            ((candidate.match(fragment), candidate) for candidate in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] == 0:
            return None
        if exact_only and ranked[0][0] < 1000:
            return None
        if len(ranked) > 1 and ranked[1][0] == ranked[0][0] and ranked[0][0] > 0:
            return None  # ambiguous, let the caller report candidates
        return ranked[0][1]

    @staticmethod
    def _display(candidate: _Candidate, world: WorldDefinition, action: str) -> str:
        if action == "move":
            for location in world.locations:
                if location.id == candidate.id:
                    return location.name
        if action in ("take", "use"):
            for item in world.items:
                if item.id == candidate.id:
                    return item.name
        if action == "open":
            for door in world.doors:
                if door.id == candidate.id:
                    return door.name
        if action in ("talk", "attack", "defend"):
            for entity in world.entities:
                if entity.id == candidate.id:
                    return entity.name
        return candidate.id

    @staticmethod
    def _move_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        locations = {location.id: location for location in world.locations}
        here = locations.get(state.location_id)
        if here is None:
            return []
        candidates = []
        for exit_def in here.exits:
            target = locations.get(exit_def.target_id)
            if target is not None:
                candidates.append(_Candidate(target.id, [target.name, target.id, exit_def.label], "location"))
        return candidates

    @staticmethod
    def _item_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        items = []
        for item in world.items:
            if state.inventory.get(item.id, 0) > 0:
                items.append(_Candidate(item.id, [item.name, item.id], "item"))
            elif state.item_locations.get(item.id) == state.location_id:
                items.append(_Candidate(item.id, [item.name, item.id], "item"))
        return items

    @staticmethod
    def _door_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        return [
            _Candidate(door.id, [door.name, door.id], "door")
            for door in world.doors
            if door.location_id == state.location_id
        ]

    @staticmethod
    def _enemy_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        """Creatures to fight: hostile NPCs, creatures, or an ongoing target."""
        candidates = []
        for entity in world.entities:
            if entity.location_id != state.location_id:
                continue
            if entity.kind == "creature" or entity.hostile:
                candidates.append(_Candidate(entity.id, [entity.name, entity.id], "entity"))
        target = state.combat.get("target_id")
        if target and all(candidate.id != target for candidate in candidates):
            entity = next((e for e in world.entities if e.id == target), None)
            if entity is not None:
                candidates.append(_Candidate(entity.id, [entity.name, entity.id], "entity"))
        return candidates

    @staticmethod
    def _talk_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        candidates = [
            _Candidate(entity.id, [entity.name, entity.id], "entity")
            for entity in world.entities
            if entity.location_id == state.location_id
        ]
        known = {entity.id for entity in world.entities}
        location = next((loc for loc in world.locations if loc.id == state.location_id), None)
        for npc_id in getattr(location, "npc_ids", []) if location else []:
            if npc_id not in known:
                candidates.append(_Candidate(npc_id, [npc_id], "entity"))
        return candidates


class TextActionService:
    """Turn one utterance into exactly one committed, idempotent action."""

    def __init__(
        self,
        game_store,
        scenario_store=None,
        action_service: Optional[ActionService] = None,
        parser: Optional[TextActionParser] = None,
    ):
        self.game_store = game_store
        self.scenario_store = scenario_store
        self.action_service = action_service or ActionService(game_store)
        self.parser = parser or TextActionParser()

    # ---------------------------------------------------------------- binding
    def bindable_world(self, campaign_id: str) -> Optional[WorldDefinition]:
        if self.game_store.get_state(campaign_id) is not None:
            return None
        if self.scenario_store is None:
            return None
        campaign = self.scenario_store.get_campaign(campaign_id)
        if campaign is None:
            return None
        for summary in self.game_store.list_worlds():
            if summary.get("scenario_id") != campaign.scenario_id:
                continue
            world = self.game_store.get_world(summary["world_id"])
            if world is not None:
                world_service.bind_world(self.game_store, campaign_id, world.world_id)
                return world
        return None

    def ensure_state(self, campaign_id: str) -> GameState:
        state = self.game_store.get_state(campaign_id)
        if state is not None:
            return state
        self.bindable_world(campaign_id)
        state = self.game_store.get_state(campaign_id)
        if state is None:
            raise LookupError(
                f"campaign '{campaign_id}' has no bound world; bind one first"
            )
        return state

    # --------------------------------------------------------------- execute
    async def execute(
        self,
        campaign_id: str,
        text: str,
        request_id: Optional[str] = None,
        narrate: bool = True,
    ) -> Dict[str, Any]:
        request_id = request_id or f"txt-{uuid.uuid4().hex}"
        replayed = self.game_store.get_request(request_id)
        if replayed is not None:
            return {
                "request_id": request_id,
                "status": "duplicate",
                "parsed": None,
                "result": replayed["result"],
            }
        state = self.ensure_state(campaign_id)
        world = world_service.get_world(self.game_store, state.world_id)
        parsed = self.parser.parse(text, world, state)
        if parsed is None or not parsed.action:
            return self._unparsed(campaign_id, request_id, text, state)
        request = ActionRequest(
            campaign_id=campaign_id,
            request_id=request_id,
            action=parsed.action,
            target_id=parsed.target_id,
            params={"text": text},
            narrate=narrate,
        )
        result = await self.action_service.execute(request)
        return {
            "request_id": request_id,
            "status": result.status,
            "parsed": parsed.as_dict(),
            "result": json.loads(result.model_dump_json()),
        }

    def _unparsed(
        self, campaign_id: str, request_id: str, text: str, state: GameState
    ) -> Dict[str, Any]:
        from app.models.game import NarrativeResult

        message = (
            "无法理解这条行动：" + text[:80]
            + "。可以尝试「看」「前往 <地点>」「拿 <物品>」「打开 <门>」「用 <物品>」「说 <人物>」「攻击 <敌人>」「防御」「逃跑」。"
        )
        result = ActionResult(
            request_id=request_id,
            campaign_id=campaign_id,
            status="rejected",
            action="unknown",
            ok=False,
            code="unparsed_action",
            message=message,
            state_version=state.version,
            narrative=NarrativeResult(status="skipped"),
        )
        payload = json.loads(result.model_dump_json())
        try:
            self.game_store.record_request(
                campaign_id=campaign_id,
                request_id=request_id,
                request_payload={"text": text, "source": "text"},
                status="rejected",
                result_payload=payload,
                state_version=state.version,
            )
        except Exception:  # noqa: BLE001 - a race with a replay must not fail the turn
            stored = self.game_store.get_request(request_id)
            if stored is not None:
                payload = stored["result"]
        return {
            "request_id": request_id,
            "status": "rejected",
            "parsed": None,
            "result": payload,
        }
