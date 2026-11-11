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
from app.engines.event_engine import condition_holds, entity_item_count, entity_location
from app.models.game import ActionRequest, ActionResult, Condition, GameState, WorldDefinition
import logging
import sqlite3

from app.services import intent_advisor, starter_worlds, world_service
from app.services.action_service import ActionService

logger = logging.getLogger(__name__)


def campaign_clock_paused(campaign_id: str) -> bool:
    """Whether the world clock of this campaign is frozen (player left)."""
    try:
        from app.api import routes_game

        return bool(routes_game.campaign_clock_paused(campaign_id))
    except Exception:  # noqa: BLE001 - no runtime means nothing is running
        return True


def _first_sentence(text: str, limit: int = 160) -> str:
    """First non-empty line of an opening narrative, trimmed for a summary."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""

#: The parser accepts exactly the actions the rule catalogue declares, and the
#: verb table is read from the same place, so the two can never drift apart.
ACTIONS = action_names()

#: Verb table. Longest phrases first so "打开" wins over "打".
_VERBS: Sequence[Tuple[str, Tuple[str, ...]]] = verb_table()

#: The canonical verb per action, used when an advisor has named an action but
#: the utterance itself never contained a word the parser recognises.
_VERBS_PRIMARY: Dict[str, str] = {
    action: verbs[0] for action, verbs in _VERBS if verbs
}

_TRAILING = ("", "。", "！", "?", "？", ".", "吧", "啊", "呢")
_LEADING_PARTICLES = ("请", "我想", "我要", "帮我", "试着", "试图", "麻烦")
_ENVELOPE = re.compile(r"^\[([A-Z]+)\]\s*", re.IGNORECASE)
_PARSE_FAILURES = {"missing_target", "ambiguous", "no_target"}


@dataclass
class ParsedAction:
    action: str
    target_id: Optional[str] = None
    matched_name: str = ""
    confidence: str = "exact"
    candidates: List[str] = field(default_factory=list)
    note: str = ""
    #: Extra ActionRequest params the parse produced (e.g. action_id for a
    #: world-authored custom action).
    params: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target_id": self.target_id,
            "matched_name": self.matched_name,
            "confidence": self.confidence,
            "candidates": list(self.candidates),
            "note": self.note,
            "params": dict(self.params),
        }


def _norm(text: str) -> str:
    value = (text or "").strip()
    envelope = _ENVELOPE.match(value)
    if envelope and envelope[1].upper() in ("DO", "SAY"):
        value = value[envelope.end():].strip()
    for prefix in _LEADING_PARTICLES:
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    return value


def _verb_prefix(text: str, verb: str) -> bool:
    if not text.startswith(verb):
        return False
    rest = text[len(verb):]
    return not (
        rest and verb[-1].isascii() and verb[-1].isalnum()
        and (rest[0].isalnum() or rest[0] == "_")
    )


#: Verbs that some authors bake into a name ("Open Door", "打开的门").
_VERB_PREFIXES = (
    "open", "unlock", "take", "pick", "use", "talk", "move", "go", "attack",
    "defend", "flee", "buy", "sell", "equip", "unequip", "choose", "look",
    "investigate", "inspect", "examine",
)


def _door_verb(name: str) -> str:
    """'Open Door' -> 'open door'; 'Inner Door' -> '' (no verb baked in)."""
    lowered = " ".join((name or "").lower().split())
    for verb in _VERB_PREFIXES:
        if lowered == verb:
            return ""
        if lowered.startswith(verb + " "):
            return lowered
    return ""


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
    __slots__ = ("id", "names", "kind", "verb")

    def __init__(self, item_id: str, names: Sequence[str], kind: str, verb: str = ""):
        self.id = item_id
        self.names = _aliases(names)
        self.kind = kind
        #: Names that describe the action as well as the object ("Open Door").
        #: Ordinary matching would strip the verb from the utterance, so these
        #: names are compared against the whole lowered utterance instead.
        self.verb = verb.strip().lower()

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
        envelope = _ENVELOPE.match((text or "").strip())
        mode = envelope[1].upper() if envelope else "DO"
        if mode not in ("DO", "SAY"):
            return None
        raw = _norm(text)
        if not raw:
            return None
        lowered = raw.lower()
        for suffix in _TRAILING:
            if suffix and lowered.endswith(suffix):
                lowered = lowered[: -len(suffix)].strip()
        action, fragment = self._split_verb(lowered)
        if mode == "SAY" and action not in (None, "talk"):
            return None
        if action is None:
            if mode == "DO":
                custom = self._split_custom_verb(lowered, world)
                if custom is not None:
                    return self._resolve_custom(custom[0], custom[1], world, state)
            guessed = self._guess_without_verb(lowered, world, state)
            return guessed if guessed and (mode == "DO" or guessed.action == "talk") else None
        parsed = self._resolve(action, fragment, world, state, utterance=text)
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
                verb = verb.rstrip()
                if _verb_prefix(text, verb):
                    return action, _clean_target(text[len(verb):])
        return None, text

    # -- world-authored actions --------------------------------------------
    @staticmethod
    def _custom_verbs(world: WorldDefinition) -> List[Tuple[str, str]]:
        """(verb, custom action id) pairs, longest phrase first."""
        pairs: List[Tuple[str, str]] = []
        for spec in getattr(world, "custom_actions", []) or []:
            for verb in spec.verbs:
                cleaned = (verb or "").strip().lower()
                if cleaned:
                    pairs.append((cleaned, spec.id))
        pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        return pairs

    def _split_custom_verb(
        self, text: str, world: WorldDefinition
    ) -> Optional[Tuple[str, str]]:
        for verb, action_id in self._custom_verbs(world):
            if _verb_prefix(text, verb):
                return action_id, _clean_target(text[len(verb):])
        return None

    def _resolve_custom(
        self, action_id: str, fragment: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        spec = next(
            (
                item
                for item in getattr(world, "custom_actions", []) or []
                if item.id == action_id
            ),
            None,
        )
        if spec is None:
            return None
        label = spec.label or spec.id
        params = {"action_id": spec.id}
        if spec.target == "none" or not fragment:
            if spec.target == "required":
                return ParsedAction(
                    action="custom",
                    confidence="missing_target",
                    note=f"{label} needs a target.",
                    params=params,
                )
            return ParsedAction(action="custom", confidence="verb_only", params=params)
        matched = self._best(fragment, self._custom_candidates(world, state))
        if matched is None:
            if spec.target == "required":
                return ParsedAction(
                    action="custom",
                    confidence="missing_target",
                    note=f"could not resolve a target from '{fragment}'",
                    params=params,
                )
            return ParsedAction(action="custom", confidence="verb_only", params=params)
        return ParsedAction(
            action="custom",
            target_id=matched.id,
            matched_name=matched.names[0] if matched.names else matched.id,
            confidence="exact",
            params=params,
        )

    def _custom_candidates(
        self, world: WorldDefinition, state: GameState
    ) -> List[_Candidate]:
        """Anything the player could name as the target of a world action."""
        candidates: List[_Candidate] = []
        seen = set()
        for candidate in (
            self._move_candidates(world, state)
            + self._item_candidates(world, state)
            + self._door_candidates(world, state)
            + self._entity_candidates(world, state)
            + self._talk_candidates(world, state)
        ):
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            candidates.append(candidate)
        return candidates

    def _guess_without_verb(
        self, text: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        candidates: List[Tuple[str, _Candidate]] = []
        candidates.extend(("move", c) for c in self._move_candidates(world, state))
        candidates.extend(("take", c) for c in self._item_candidates(world, state))
        candidates.extend(("talk", c) for c in self._talk_candidates(world, state))
        candidates.extend(("open", c) for c in self._door_candidates(world, state))
        matches = {
            (action, candidate.id): candidate
            for action, candidate in candidates
            if candidate.match(text) >= 1000
        }
        if len(matches) != 1:
            return None
        (action, _target_id), candidate = next(iter(matches.items()))
        return ParsedAction(
            action=action,
            target_id=candidate.id,
            matched_name=self._display(candidate, world, action),
            confidence="name_only",
        )

    def _resolve(
        self,
        action: str,
        fragment: str,
        world: WorldDefinition,
        state: GameState,
        utterance: str = "",
    ) -> Optional[ParsedAction]:
        if action == "look":
            if fragment in ("", "around", "周围", "四周"):
                return ParsedAction(action="look", confidence="verb_only")
            return ParsedAction(action="look", target_id=fragment)
        if action == "flee":
            return ParsedAction(action="flee", confidence="verb_only")
        if action in ("attack", "defend"):
            enemies = self._enemy_candidates(world, state)
            matched = None
            if fragment:
                candidates = self._entity_candidates(world, state)
                matched = self._best(fragment, candidates, exact_only=True)
                if matched is None and fragment.startswith("the "):
                    matched = self._best(fragment[4:], candidates, exact_only=True)
                if matched is None:
                    return ParsedAction(
                        action=action,
                        confidence="missing_target",
                        note=f"could not resolve an unambiguous target from '{fragment}'",
                    )
            elif action == "attack":
                matched = enemies[0] if len(enemies) == 1 else None
                if matched is None:
                    return ParsedAction(
                        action=action,
                        confidence="ambiguous" if enemies else "no_target",
                        note="Name one living enemy here to attack.",
                    )
            return ParsedAction(
                action=action,
                target_id=matched.id if matched else None,
                matched_name=self._display(matched, world, action) if matched else "",
                confidence="exact" if matched else "verb_only",
            )
        if action in ("buy", "sell"):
            candidates = (
                self._shop_candidates(world, state)
                if action == "buy"
                else self._item_candidates(world, state)
            )
            matched = self._best(fragment, candidates) if fragment else None
            if matched is None:
                return ParsedAction(
                    action=action,
                    confidence="missing_target",
                    note=f"could not resolve a target from '{fragment}'",
                )
            return ParsedAction(
                action=action,
                target_id=matched.id,
                matched_name=self._display(matched, world, action),
                confidence="exact",
            )
        if action == "investigate":
            if not fragment:
                return ParsedAction(action="investigate", confidence="verb_only")
            candidates = (
                self._entity_candidates(world, state)
                + self._item_candidates(world, state)
                + self._door_candidates(world, state)
                + self._move_candidates(world, state)
            )
            matched = self._best(fragment, candidates)
            if matched is None:
                return ParsedAction(
                    action="investigate",
                    confidence="missing_target",
                    note=f"could not resolve a target from '{fragment}'",
                )
            return ParsedAction(
                action="investigate",
                target_id=matched.id,
                matched_name=self._display(matched, world, "investigate"),
                confidence="exact",
            )
        if action == "talk" and not fragment:
            return ParsedAction(action="talk", confidence="verb_only")
        if action == "move":
            rules = ("move", self._move_candidates(world, state))
        elif action in ("take", "use", "equip"):
            rules = (action, self._item_candidates(world, state))
        elif action == "unequip":
            if not fragment:
                return ParsedAction(action="unequip", confidence="verb_only")
            rules = ("unequip", self._item_candidates(world, state))
        elif action == "open":
            rules = ("open", self._door_candidates(world, state))
        elif action == "talk":
            rules = ("talk", self._talk_candidates(world, state))
        else:
            return None
        matched = self._best(fragment, rules[1], whole=utterance or fragment)
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
        fragment: str,
        candidates: List[_Candidate],
        exact_only: bool = False,
        whole: str = "",
    ) -> Optional[_Candidate]:
        def score(candidate: "_Candidate") -> int:
            best = candidate.match(fragment)
            if whole and candidate.verb:
                # The authored name already contains the verb, so the verb was
                # stripped from the utterance before matching; compare against
                # the full sentence instead.
                if _norm(whole) == _norm(candidate.verb) or _norm(whole).startswith(candidate.verb + " "):
                    if not fragment or fragment in candidate.verb or candidate.verb.endswith(fragment):
                        best = max(best, 900)
            return best

        ranked = sorted(
            ((score(candidate), candidate) for candidate in candidates),
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
        if action in ("buy", "sell"):
            candidates = (
                self._shop_candidates(world, state)
                if action == "buy"
                else self._item_candidates(world, state)
            )
            matched = self._best(fragment, candidates) if fragment else None
            if matched is None:
                return ParsedAction(
                    action=action,
                    confidence="missing_target",
                    note=f"could not resolve a target from '{fragment}'",
                )
            return ParsedAction(
                action=action,
                target_id=matched.id,
                matched_name=self._display(matched, world, action),
                confidence="exact",
            )
        if action == "investigate":
            for collection in (world.entities, world.items, world.doors, world.locations):
                for entry in collection:
                    if entry.id == candidate.id:
                        return entry.name
        if action == "move":
            for location in world.locations:
                if location.id == candidate.id:
                    return location.name
        if action in ("take", "use", "equip", "unequip", "buy", "sell"):
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
            elif any(
                entity_location(state, world, entity.id) == state.location_id
                and entity_item_count(state, world, entity.id, item.id) > 0
                for entity in world.entities
            ):
                items.append(_Candidate(item.id, [item.name, item.id], "item"))
        return items

    @staticmethod
    def _shop_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        items = {item.id: item for item in world.items}
        seen = set()
        candidates = []
        for entity in world.entities:
            if entity_location(state, world, entity.id) != state.location_id:
                continue
            for item_id in entity.shop_items:
                if item_id in seen or item_id not in items:
                    continue
                seen.add(item_id)
                candidates.append(
                    _Candidate(item_id, [items[item_id].name, item_id], "item")
                )
        return candidates

    @staticmethod
    def _door_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        return [
            _Candidate(door.id, [door.name, door.id], "door", verb=_door_verb(door.name))
            for door in world.doors
            if door.location_id == state.location_id
        ]

    @staticmethod
    def _entity_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        return [
            _Candidate(entity.id, [entity.name, entity.id], entity.kind)
            for entity in world.entities
            if entity_location(state, world, entity.id) == state.location_id
            and condition_holds(state, world, Condition(kind="entity_alive", target=entity.id))
        ]

    @classmethod
    def _enemy_candidates(cls, world: WorldDefinition, state: GameState) -> List[_Candidate]:
        eligible = {
            entity.id for entity in world.entities
            if entity.kind != "object"
            and (entity.kind == "creature" or entity.hostile
                 or entity.id == state.combat.get("target_id"))
        }
        return [c for c in cls._entity_candidates(world, state) if c.id in eligible]

    @classmethod
    def _talk_candidates(cls, world: WorldDefinition, state: GameState) -> List[_Candidate]:
        candidates = [
            candidate for candidate in cls._entity_candidates(world, state)
            if candidate.kind != "object"
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
        advisor: Optional[intent_advisor.IntentAdvisor] = None,
    ):
        self.game_store = game_store
        self.scenario_store = scenario_store
        self.action_service = action_service or ActionService(
            game_store,
            clock_paused=lambda campaign_id: campaign_clock_paused(campaign_id),
        )
        self.parser = parser or TextActionParser()
        #: Optional LLM that may *suggest* a mapping when the parser finds none.
        #: Off unless ENGINE_INTENT_ADVISOR is set; even when on, its suggestion
        #: is re-parsed and re-validated, and the rule engine still decides.
        self.advisor = advisor

    # ---------------------------------------------------------------- binding
    def bindable_world(self, campaign_id: str) -> Optional[WorldDefinition]:
        """Return the world for a campaign, binding (or creating) one if needed.

        Resolution order, all deterministic:
        1. an already bound campaign keeps its world (never rebound here);
        2. a world authored for the campaign's scenario, in creation order;
        3. a starter world generated from the scenario itself.

        Step 3 is what keeps a brand new campaign inside the rules lane: without
        it the caller fell back to the legacy LLM session, whose state this
        store cannot see, so the two systems disagreed about the same player.
        """
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
        return self._provision_starter_world(campaign_id, campaign)

    def _scenario_record(self, scenario_id: str):
        if self.scenario_store is None or not hasattr(self.scenario_store, "get_scenario"):
            return None
        try:
            return self.scenario_store.get_scenario(scenario_id)
        except Exception:  # noqa: BLE001 - a missing scenario falls back to defaults
            logger.warning("could not read scenario %s", scenario_id, exc_info=True)
            return None

    def _provision_starter_world(self, campaign_id: str, campaign) -> Optional[WorldDefinition]:
        """Create and bind a starter world for a campaign that has none.

        The document is generated as plain data and validated with the same
        `validate_world` any authored world goes through; an invalid document is
        never saved. A world id that already exists is skipped (never
        overwritten), and a campaign that already has state is left alone.
        """
        scenario = self._scenario_record(getattr(campaign, "scenario_id", ""))
        language = getattr(scenario, "language", "") or ""
        title = getattr(scenario, "title", "") or ""
        summary = (
            getattr(scenario, "description", "")
            or _first_sentence(getattr(scenario, "opening_narrative", ""))
            or ""
        )
        world_type = getattr(scenario, "world_type", "") or ""
        base_id = starter_worlds.starter_world_id(campaign_id)
        for attempt in range(0, 6):
            world_id = starter_worlds.alternate_world_id(base_id, attempt)
            if self.game_store.get_world(world_id) is not None:
                continue
            document = starter_worlds.build_world_document(
                scenario_id=getattr(campaign, "scenario_id", ""),
                world_id=world_id,
                title=title,
                summary=summary,
                language=language,
                world_type=world_type,
            )
            try:
                world = WorldDefinition(**document)
            except Exception:  # noqa: BLE001 - never bind an invalid generated world
                logger.error(
                    "generated starter world for campaign %s failed model validation",
                    campaign_id, exc_info=True,
                )
                return None
            errors = world_service.validate_world(world)
            if errors:
                logger.error(
                    "generated starter world for campaign %s is invalid: %s",
                    campaign_id, "; ".join(errors),
                )
                return None
            world_service.save_world(self.game_store, world)
            state = world_service.bind_world(self.game_store, campaign_id, world.world_id)
            logger.info(
                "campaign %s auto-bound generated starter world %s (%s)",
                campaign_id, world.world_id, world.name,
            )
            del state
            return world
        logger.error("no free starter world id for campaign %s", campaign_id)
        return None

    def _world_for_state(self, state: GameState, campaign_id: str) -> WorldDefinition:
        """The world a bound state points at; a starter world if it went missing.

        A state can outlive its world document (a world deleted while a campaign
        still points at it). Rather than failing the turn, the campaign keeps its
        progress and gets a freshly generated starter world of the same shape.
        """
        try:
            return world_service.get_world(self.game_store, state.world_id)
        except LookupError:
            logger.error(
                "campaign %s points at missing world %s; provisioning a starter",
                campaign_id, state.world_id,
            )
        campaign = (
            self.scenario_store.get_campaign(campaign_id)
            if self.scenario_store is not None else None
        )
        world = self._provision_starter_world(campaign_id, campaign) if campaign else None
        if world is None:
            raise LookupError(f"campaign '{campaign_id}' has no usable world")
        return world

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
            return await self._replay_text(campaign_id, text, request_id, narrate, replayed)
        state = self.ensure_state(campaign_id)
        world = self._world_for_state(state, campaign_id)
        parsed = self.parser.parse(text, world, state)
        advised = None
        if parsed is None or not parsed.action:
            advised = await self._advise(text, world, state, campaign_id)
            if advised is not None:
                parsed = advised
        if parsed is None or not parsed.action or parsed.confidence in _PARSE_FAILURES:
            return await self._reject_parse(campaign_id, request_id, text, state, parsed)
        request = ActionRequest(
            campaign_id=campaign_id,
            request_id=request_id,
            action=parsed.action,
            target_id=parsed.target_id,
            params={**parsed.params, "text": text},
            narrate=narrate,
        )
        result = await self.action_service.execute(request)
        return {
            "request_id": request_id,
            "status": result.status,
            "parsed": parsed.as_dict(),
            "result": json.loads(result.model_dump_json()),
        }

    async def _replay_text(
        self, campaign_id: str, text: str, request_id: str, narrate: bool, row: Dict[str, Any]
    ) -> Dict[str, Any]:
        payload = row["request"]
        current = self.game_store.get_state(campaign_id)
        if (
            row["campaign_id"] != campaign_id
            or payload.get("campaign_id") != campaign_id
            or payload.get("actor_id") != "player"
            or payload.get("params", {}).get("text") != text
        ):
            result = ActionResult(
                request_id=request_id,
                campaign_id=campaign_id,
                status="conflict",
                action="unknown",
                code="request_id_conflict",
                message="request_id was already used for a different campaign or payload",
                state_version=current.version if current is not None else 0,
                state=current,
            )
        elif payload.get("action") == "unknown":
            result = self.action_service._load_result(row)
            result.status = "duplicate"
            result.message = "duplicate request ignored; no side effects were applied"
            result.state_version = current.version if current is not None else row["state_version"]
            result.state = current
        else:
            request = ActionRequest.model_validate({**payload, "narrate": narrate})
            result = await self.action_service.execute(request)
        return {
            "request_id": request_id,
            "status": result.status,
            "parsed": None,
            "result": json.loads(result.model_dump_json()),
        }

    def _intent_router(self, campaign_id: str):
        """Router from the scenario's intent preset, or None to keep the global one."""
        if self.scenario_store is None:
            return None
        try:
            campaign = self.scenario_store.get_campaign(campaign_id)
            if campaign is None:
                return None
            config = self.scenario_store.get_play_config(campaign.scenario_id)
            preset = (
                self.scenario_store.get_model_preset(config.intent_preset_id)
                if config.intent_preset_id
                else None
            )
            if preset is None or preset.role not in ("intent", "any") or not preset.provider:
                return None
            from app.services.narrative_service import preset_router

            return preset_router(
                {"provider": preset.provider, "model": preset.model}
            )
        except Exception:  # noqa: BLE001 - advice is optional
            return None

    async def _advise(
        self, text: str, world: WorldDefinition, state: GameState, campaign_id: str
    ) -> Optional[ParsedAction]:
        """Second chance for an utterance the deterministic parser did not map.

        The suggestion is fed back through the same parser, so the model can
        only ever steer *which* verb table entry is tried - the resolution of
        the target, the preconditions and the effects stay in code.
        """
        envelope = _ENVELOPE.match((text or "").strip())
        mode = envelope[1].upper() if envelope else "DO"
        if mode not in ("DO", "SAY") or not intent_advisor.enabled():
            return None
        advisor = self.advisor
        router = self._intent_router(campaign_id) if advisor is not None else None
        if router is not None:
            advisor = intent_advisor.IntentAdvisor(router)
        if advisor is None:
            logger.info("intent advisor is enabled but not wired; keeping the rejection")
            return None
        extra = [
            {"name": action.id, "description": action.label or action.description}
            for action in getattr(world, "custom_actions", []) or []
        ]
        try:
            suggestion = await advisor.suggest(text, world, extra)
        except Exception as exc:  # noqa: BLE001 - advice must never break a turn
            logger.info("intent advisor failed: %s", exc)
            return None
        if suggestion is None or (mode == "SAY" and suggestion.action != "talk"):
            return None
        # The model names an action and the words it thinks name the target.
        # Rebuild a verb+target utterance from exactly those two pieces: the
        # parser then resolves the target against the real world, so an
        # invented target cannot survive and the verb table stays in charge.
        primary = _VERBS_PRIMARY.get(suggestion.action, "")
        candidate = f"{primary} {suggestion.fragment}".strip() if primary else ""
        if not candidate:
            return None
        parsed = self.parser.parse(candidate, world, state)
        if parsed is None or not parsed.action:
            return None
        if parsed.action != suggestion.action and suggestion.action != "custom":
            # The parser disagrees with the model about which verb this is. The
            # parser wins: it matched real words against the real verb table.
            logger.info(
                "intent advisor suggested %s, parser says %s; keeping the parser",
                suggestion.action,
                parsed.action,
            )
        parsed.note = (parsed.note + " " if parsed.note else "") + "intent advisor"
        if parsed.confidence not in _PARSE_FAILURES:
            parsed.confidence = "advised"
        return parsed

    async def _reject_parse(
        self, campaign_id: str, request_id: str, text: str, state: GameState,
        parsed: Optional[ParsedAction] = None,
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
            action=parsed.action if parsed else "unknown",
            ok=False,
            code=parsed.confidence if parsed else "unparsed_action",
            message=parsed.note if parsed else message,
            state_version=state.version,
            state=state,
            narrative=NarrativeResult(status="skipped"),
        )
        request_payload = {
            "campaign_id": campaign_id,
            "request_id": request_id,
            "action": result.action,
            "actor_id": "player",
            "params": {**(parsed.params if parsed else {}), "text": text},
        }
        payload = json.loads(result.model_dump_json())
        try:
            self.game_store.record_request(
                campaign_id=campaign_id,
                request_id=request_id,
                request_payload=request_payload,
                status="rejected",
                result_payload=payload,
                state_version=state.version,
            )
        except sqlite3.IntegrityError:
            stored = self.game_store.get_request(request_id)
            if stored is None:
                raise
            return await self._replay_text(campaign_id, text, request_id, False, stored)
        return {
            "request_id": request_id,
            "status": "rejected",
            "parsed": parsed.as_dict() if parsed else None,
            "result": payload,
        }
