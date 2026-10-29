"""Validated data models for the deterministic game engine.

Architecture contract (see docs/audit_phase0.md):

* rules decide facts      -> RuleOutcome (code + message + effects)
* effects record them     -> Effect, applied by engines/event_engine.py
* world state keeps it    -> GameState, persisted as a versioned snapshot
* the LLM only narrates   -> NarrativeResult, committed facts only

Everything here is plain, validated data. No configuration is ever executed
as code: unknown condition/effect kinds fail Pydantic validation instead.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ActionKind(str, Enum):
    """Actions the deterministic pipeline currently understands."""

    MOVE = "move"
    LOOK = "look"
    TAKE = "take"
    OPEN = "open"
    USE = "use"
    TALK = "talk"


class EffectKind(str, Enum):
    SET_FLAG = "set_flag"
    GIVE_ITEM = "give_item"
    REMOVE_ITEM = "remove_item"
    MOVE_PLAYER = "move_player"
    OPEN_DOOR = "open_door"
    CLOSE_DOOR = "close_door"
    QUEST_PROGRESS = "quest_progress"
    QUEST_COMPLETE = "quest_complete"
    EMIT = "emit"


class ConditionKind(str, Enum):
    FLAG = "flag"
    HAS_ITEM = "has_item"
    ITEM_COUNT = "item_count"
    DOOR_OPEN = "door_open"
    AT_LOCATION = "at_location"
    QUEST_STATUS = "quest_status"


# ---------------------------------------------------------------------------
# World definition (authored configuration, not hardcoded content)
# ---------------------------------------------------------------------------


class _ConfigModel(BaseModel):
    """Base for authored world configuration: unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


class ExitDef(_ConfigModel):
    """A traversable connection between two locations."""

    target_id: str = Field(..., min_length=1, max_length=64)
    label: str = ""
    door_id: Optional[str] = None
    requires_item_id: Optional[str] = None


class LocationDef(_ConfigModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    exits: List[ExitDef] = Field(default_factory=list)
    door_ids: List[str] = Field(default_factory=list)
    item_ids: List[str] = Field(default_factory=list)
    npc_ids: List[str] = Field(default_factory=list)


class ItemDef(_ConfigModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    stackable: bool = True


class EntityDef(_ConfigModel):
    """An authored non-player entity: NPC, creature or interactive object.

    Only the fixed configuration lives here. Where the entity currently is,
    what it carries and how it feels are authoritative runtime state and are
    deliberately not stored in the world document.
    """

    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    location_id: str = Field(..., min_length=1, max_length=64)
    kind: Literal["npc", "creature", "object"] = "npc"
    dialogue: List[str] = Field(default_factory=list)
    hostile: bool = False
    attributes: Dict[str, Any] = Field(default_factory=dict)


class DoorDef(_ConfigModel):
    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    location_id: str = Field(..., min_length=1, max_length=64)
    locked: bool = True
    opened: bool = False
    key_item_id: Optional[str] = None
    consumes_key: bool = False


class Condition(_ConfigModel):
    """A single, checked predicate over the authoritative state."""

    kind: Literal[
        "flag",
        "has_item",
        "item_count",
        "door_open",
        "at_location",
        "quest_status",
        "combatant_hp",
        "combatant_alive",
        "entity_at",
        "entity_alive",
    ]
    target: str = ""
    op: Literal["eq", "ne", "gte", "lte", "gt", "lt"] = "eq"
    value: Any = True


class Effect(_ConfigModel):
    """A single, checked state mutation."""

    kind: Literal[
        "set_flag",
        "give_item",
        "remove_item",
        "move_player",
        "open_door",
        "close_door",
        "quest_progress",
        "quest_complete",
        "combat_hp",
        "player_damage",
        "move_entity",
        "seed_entity",
        "emit",
    ]
    target: str = ""
    value: Any = None
    amount: int = 1


class CustomActionDef(_ConfigModel):
    """An action a world declares for itself: conditions plus effects.

    Configuration can *describe* an action but never supply an implementation:
    `requires` holds the same validated Condition rows events use and
    `effects` the same validated Effect rows, both decided by the same engine.
    Nothing here is ever executed as code.
    """

    id: str = Field(..., min_length=1, max_length=64)
    label: str = Field(default="", max_length=120)
    description: str = ""
    #: Words the deterministic text parser maps onto this action, e.g.
    #: ["祈祷", "pray"]. Verbs must not shadow a built-in action's verbs.
    verbs: List[str] = Field(default_factory=list)
    target: Literal["none", "required", "optional"] = "none"
    #: Preconditions; all of them must hold for the action to be allowed.
    requires: List[Condition] = Field(default_factory=list)
    #: What the action does once it is allowed.
    effects: List[Effect] = Field(default_factory=list)


class ObjectiveDef(_ConfigModel):
    id: str = Field(..., min_length=1, max_length=64)
    kind: Literal["obtain_item", "open_door", "reach_location", "flag"]
    target: str = Field(..., min_length=1, max_length=64)
    required_count: int = Field(default=1, ge=1)
    description: str = ""


class QuestDef(_ConfigModel):
    id: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    objectives: List[ObjectiveDef] = Field(default_factory=list)
    rewards: List[Effect] = Field(default_factory=list)
    enabled: bool = True


class GameEventDef(_ConfigModel):
    """A configurable event: validated conditions plus validated effects."""

    id: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    enabled: bool = True
    trigger: Literal["action", "state_change"] = "action"
    trigger_action: Optional[str] = None
    trigger_target: Optional[str] = None
    conditions: List[Condition] = Field(default_factory=list)
    effects: List[Effect] = Field(default_factory=list)
    priority: int = 0
    repeat: Literal["once", "always"] = "once"


class WorldDefinition(BaseModel):
    """A complete, configurable world document."""

    model_config = ConfigDict(extra="forbid")

    world_id: str = Field(..., min_length=1, max_length=64)
    scenario_id: str = ""
    name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    start_location_id: str = Field(..., min_length=1, max_length=64)
    locations: List[LocationDef] = Field(default_factory=list)
    items: List[ItemDef] = Field(default_factory=list)
    doors: List[DoorDef] = Field(default_factory=list)
    entities: List[EntityDef] = Field(default_factory=list)
    quests: List[QuestDef] = Field(default_factory=list)
    events: List[GameEventDef] = Field(default_factory=list)
    custom_actions: List[CustomActionDef] = Field(default_factory=list)

    def validate_references(self) -> List[str]:
        """Return human-readable reference errors (empty list == valid).

        Structural validation already happened in Pydantic; this checks that
        every cross-reference points at something that exists, so an invalid
        configuration is rejected at authoring time instead of failing later
        at play time.
        """
        errors: List[str] = []
        location_ids = {loc.id for loc in self.locations}
        item_ids = {item.id for item in self.items}
        door_ids = {door.id for door in self.doors}
        quest_ids = {quest.id for quest in self.quests}
        entity_ids = {entity.id for entity in self.entities}

        for name, ids in (
            ("location", [loc.id for loc in self.locations]),
            ("item", [item.id for item in self.items]),
            ("door", [door.id for door in self.doors]),
            ("quest", [quest.id for quest in self.quests]),
            ("event", [event.id for event in self.events]),
        ):
            seen = set()
            for value in ids:
                if value in seen:
                    errors.append(f"duplicate {name} id '{value}'")
                seen.add(value)

        if self.start_location_id not in location_ids:
            errors.append(
                f"start_location_id '{self.start_location_id}' is not a known location"
            )

        for loc in self.locations:
            for exit_def in loc.exits:
                if exit_def.target_id not in location_ids:
                    errors.append(
                        f"location '{loc.id}' exit target '{exit_def.target_id}' is unknown"
                    )
                if exit_def.door_id and exit_def.door_id not in door_ids:
                    errors.append(
                        f"location '{loc.id}' exit door '{exit_def.door_id}' is unknown"
                    )
                if (
                    exit_def.requires_item_id
                    and exit_def.requires_item_id not in item_ids
                ):
                    errors.append(
                        f"location '{loc.id}' exit requires unknown item "
                        f"'{exit_def.requires_item_id}'"
                    )
            for door_id in loc.door_ids:
                if door_id not in door_ids:
                    errors.append(
                        f"location '{loc.id}' references unknown door '{door_id}'"
                    )
            for item_id in loc.item_ids:
                if item_id not in item_ids:
                    errors.append(
                        f"location '{loc.id}' references unknown item '{item_id}'"
                    )

        entity_ids: set = set()
        for entity in self.entities:
            if entity.id in entity_ids:
                errors.append(f"duplicate entity id '{entity.id}'")
            entity_ids.add(entity.id)
            if entity.id in item_ids or entity.id in door_ids or entity.id in location_ids:
                errors.append(
                    f"entity id '{entity.id}' collides with an item, door or location id"
                )
            if entity.location_id not in location_ids:
                errors.append(
                    f"entity '{entity.id}' is in unknown location '{entity.location_id}'"
                )
        # LocationDef.npc_ids is a legacy display list and may name entities that
        # were never authored as EntityDef rows, so it is intentionally not
        # validated against entity_ids.

        for door in self.doors:
            if door.location_id not in location_ids:
                errors.append(
                    f"door '{door.id}' is in unknown location '{door.location_id}'"
                )
            if door.key_item_id and door.key_item_id not in item_ids:
                errors.append(
                    f"door '{door.id}' requires unknown key item '{door.key_item_id}'"
                )

        for quest in self.quests:
            for objective in quest.objectives:
                if objective.kind == "obtain_item" and objective.target not in item_ids:
                    errors.append(
                        f"quest '{quest.id}' objective '{objective.id}' targets "
                        f"unknown item '{objective.target}'"
                    )
                if objective.kind == "open_door" and objective.target not in door_ids:
                    errors.append(
                        f"quest '{quest.id}' objective '{objective.id}' targets "
                        f"unknown door '{objective.target}'"
                    )
                if (
                    objective.kind == "reach_location"
                    and objective.target not in location_ids
                ):
                    errors.append(
                        f"quest '{quest.id}' objective '{objective.id}' targets "
                        f"unknown location '{objective.target}'"
                    )
            for reward in quest.rewards:
                errors.extend(
                    f"quest '{quest.id}' reward: {msg}"
                    for msg in self._validate_effect(
                        reward, item_ids, door_ids, quest_ids, entity_ids, location_ids
                    )
                )

        for event in self.events:
            for condition in event.conditions:
                errors.extend(
                    f"event '{event.id}' condition: {msg}"
                    for msg in self._validate_condition(
                        condition,
                        item_ids,
                        door_ids,
                        location_ids,
                        quest_ids,
                        entity_ids,
                    )
                )
            for effect in event.effects:
                errors.extend(
                    f"event '{event.id}' effect: {msg}"
                    for msg in self._validate_effect(
                        effect, item_ids, door_ids, quest_ids, entity_ids, location_ids
                    )
                )
            if event.trigger == "action" and event.trigger_action is None:
                errors.append(
                    f"event '{event.id}' uses trigger 'action' but has no trigger_action"
                )
            if event.trigger_target and event.trigger_action in ("open", "take"):
                known = door_ids if event.trigger_action == "open" else item_ids
                if event.trigger_target not in known:
                    errors.append(
                        f"event '{event.id}' trigger_target '{event.trigger_target}' "
                        f"is unknown for action '{event.trigger_action}'"
                    )
        errors.extend(
            self._validate_custom_actions(item_ids, door_ids, location_ids, quest_ids, entity_ids)
        )
        return errors

    def _validate_custom_actions(
        self,
        item_ids: set,
        door_ids: set,
        location_ids: set,
        quest_ids: set,
        entity_ids: set,
    ) -> List[str]:
        """Check world-authored actions: ids, verbs and their conditions/effects."""
        errors: List[str] = []
        if not self.custom_actions:
            return errors
        # Imported lazily: the catalogue is the single source of truth for the
        # built-in verbs, but the model module must not import the engine layer
        # at module import time.
        from app.engines.action_registry import action_names, verb_table

        builtin_actions = set(action_names())
        builtin_verbs = {
            verb.strip().lower()
            for _action, verbs in verb_table()
            for verb in verbs
            if verb.strip()
        }
        seen_ids = set()
        verb_owner: Dict[str, str] = {}
        for custom in self.custom_actions:
            if custom.id in seen_ids:
                errors.append(f"duplicate custom action id '{custom.id}'")
            seen_ids.add(custom.id)
            if custom.id in builtin_actions:
                errors.append(
                    f"custom action '{custom.id}' collides with a built-in action name"
                )
            if not custom.verbs:
                errors.append(
                    f"custom action '{custom.id}' declares no verbs, so no player "
                    "utterance could ever reach it"
                )
            for verb in custom.verbs:
                cleaned = (verb or "").strip().lower()
                if not cleaned:
                    errors.append(f"custom action '{custom.id}' has an empty verb")
                    continue
                if cleaned in builtin_verbs:
                    errors.append(
                        f"custom action '{custom.id}' verb '{cleaned}' is already "
                        "mapped to a built-in action"
                    )
                owner = verb_owner.get(cleaned)
                if owner is not None and owner != custom.id:
                    errors.append(
                        f"custom action '{custom.id}' verb '{cleaned}' is already "
                        f"used by custom action '{owner}'"
                    )
                verb_owner[cleaned] = custom.id
            for condition in custom.requires:
                errors.extend(
                    f"custom action '{custom.id}' condition: {msg}"
                    for msg in self._validate_condition(
                        condition,
                        item_ids,
                        door_ids,
                        location_ids,
                        quest_ids,
                        entity_ids,
                    )
                )
            for effect in custom.effects:
                errors.extend(
                    f"custom action '{custom.id}' effect: {msg}"
                    for msg in self._validate_effect(
                        effect, item_ids, door_ids, quest_ids, entity_ids, location_ids
                    )
                )
        return errors

    @staticmethod
    def _validate_condition(
        condition: Condition,
        item_ids: set,
        door_ids: set,
        location_ids: set,
        quest_ids: set,
        entity_ids: Optional[set] = None,
    ) -> List[str]:
        errors: List[str] = []
        if condition.kind in ("has_item", "item_count") and condition.target not in item_ids:
            errors.append(f"unknown item '{condition.target}'")
        if condition.kind == "door_open" and condition.target not in door_ids:
            errors.append(f"unknown door '{condition.target}'")
        if condition.kind == "at_location" and condition.target not in location_ids:
            errors.append(f"unknown location '{condition.target}'")
        if condition.kind == "quest_status":
            if condition.target not in quest_ids:
                errors.append(f"unknown quest '{condition.target}'")
            elif condition.value not in ("inactive", "active", "completed", "failed"):
                errors.append(f"unknown quest status '{condition.value}'")
        if condition.kind in ("has_item", "item_count") and condition.op not in ("eq", "gte"):
            errors.append("item conditions only support op 'eq' or 'gte'")
        if condition.kind in ("combatant_hp", "combatant_alive", "entity_alive"):
            known_entities = entity_ids if entity_ids is not None else set()
            if condition.target not in known_entities:
                errors.append(f"unknown entity '{condition.target}'")
        if condition.kind == "entity_at":
            known_entities = entity_ids if entity_ids is not None else set()
            if condition.target not in known_entities:
                errors.append(f"unknown entity '{condition.target}'")
            if condition.value not in location_ids:
                errors.append(f"unknown location '{condition.value}'")
        return errors

    @staticmethod
    def _validate_effect(
        effect: Effect,
        item_ids: set,
        door_ids: set,
        quest_ids: set,
        entity_ids: Optional[set] = None,
        location_ids: Optional[set] = None,
    ) -> List[str]:
        errors: List[str] = []
        if effect.kind in ("give_item", "remove_item") and effect.target not in item_ids:
            errors.append(f"unknown item '{effect.target}'")
        if effect.kind in ("open_door", "close_door") and effect.target not in door_ids:
            errors.append(f"unknown door '{effect.target}'")
        if effect.kind in ("quest_progress", "quest_complete") and effect.target not in quest_ids:
            errors.append(f"unknown quest '{effect.target}'")
        if effect.kind == "quest_progress" and not isinstance(effect.value, str):
            errors.append("quest_progress requires value = objective id")
        if effect.kind == "set_flag" and not effect.target:
            errors.append("set_flag requires a target flag name")
        if effect.kind == "move_player" and not effect.target:
            errors.append("move_player requires a target location id")
        if effect.kind == "combat_hp":
            known = entity_ids if entity_ids is not None else set()
            if effect.target not in known:
                errors.append(f"unknown entity '{effect.target}'")
        if effect.kind == "player_damage":
            if int(effect.amount or 0) <= 0:
                errors.append("player_damage requires a positive amount")
        if effect.kind == "seed_entity":
            known = entity_ids if entity_ids is not None else set()
            if effect.target not in known:
                errors.append(f"unknown entity '{effect.target}'")
        if effect.kind == "move_entity":
            known = entity_ids if entity_ids is not None else set()
            if effect.target not in known:
                errors.append(f"unknown entity '{effect.target}'")
            if effect.value not in (location_ids or set()):
                errors.append(f"unknown location '{effect.value}'")
        return errors


# ---------------------------------------------------------------------------
# Authoritative run-time state
# ---------------------------------------------------------------------------


class QuestProgress(BaseModel):
    quest_id: str
    status: Literal["inactive", "active", "completed", "failed"] = "active"
    objectives: Dict[str, int] = Field(default_factory=dict)


class CombatantState(BaseModel):
    """Authoritative combat state for one entity in this campaign.

    The numbers are seeded from EntityDef.attributes (max_hp/attack/defense/
    flee_chance) the first time the entity is fought, so authored world
    documents keep their existing format.
    """

    entity_id: str
    name: str = ""
    hp: int = 1
    max_hp: int = 1
    alive: bool = True
    attack: int = 1
    defense: int = 0
    flee_chance: float = 0.5


class EntityState(BaseModel):
    """Run-time state for one world entity inside one campaign.

    Authored EntityDef rows describe where an NPC *starts* and what it is;
    this record is what is true *now*: where it is, whether it is alive and any
    flags events hung on it. It only materialises once something changes the
    entity, so an untouched campaign projects straight from the world document
    and older saves (which have no `entities` key) stay valid.
    """

    entity_id: str
    location_id: str = ""
    hp: int = 1
    max_hp: int = 1
    alive: bool = True
    flags: Dict[str, Any] = Field(default_factory=dict)


class GameState(BaseModel):
    """The single authoritative snapshot for one campaign."""

    campaign_id: str
    world_id: str
    version: int = 0
    turn: int = 0
    location_id: str = ""
    inventory: Dict[str, int] = Field(default_factory=dict)
    flags: Dict[str, Any] = Field(default_factory=dict)
    doors: Dict[str, bool] = Field(default_factory=dict)
    item_locations: Dict[str, str] = Field(default_factory=dict)
    quests: Dict[str, QuestProgress] = Field(default_factory=dict)
    fired_events: List[str] = Field(default_factory=list)
    #: Storyline nodes entered so far (append-only; empty in free mode and for
    #: campaigns created before storyline support).
    story_nodes: List[str] = Field(default_factory=list)
    #: Enemy vitals and the current combat encounter, if any.
    entities: Dict[str, EntityState] = Field(default_factory=dict)
    combatants: Dict[str, CombatantState] = Field(default_factory=dict)
    combat: Dict[str, Any] = Field(default_factory=dict)
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Action pipeline contracts
# ---------------------------------------------------------------------------


class ActionRequest(BaseModel):
    campaign_id: str = Field(..., min_length=1, max_length=64)
    request_id: str = Field(..., min_length=1, max_length=128)
    action: Literal[
        "move",
        "look",
        "take",
        "open",
        "use",
        "talk",
        "attack",
        "defend",
        "flee",
        "custom",
    ]
    actor_id: str = "player"
    target_id: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    # Narration is a presentation concern, not a gameplay one: it defaults on
    # (an RPG turn should answer in prose) but never affects committed state.
    narrate: bool = True


class RuleOutcome(BaseModel):
    ok: bool
    code: str = ""
    message: str = ""
    effects: List[Effect] = Field(default_factory=list)


class EventExecution(BaseModel):
    event_id: str
    name: str = ""
    status: Literal["fired", "failed", "skipped"] = "fired"
    reason: str = ""
    effects: List[Effect] = Field(default_factory=list)


class NarrativeResult(BaseModel):
    status: Literal["skipped", "ok", "failed"] = "skipped"
    text: str = ""
    error: str = ""
    model: str = ""


class ActionResult(BaseModel):
    request_id: str
    campaign_id: str
    status: Literal["applied", "rejected", "duplicate", "conflict"]
    action: str
    actor_id: str = "player"
    target_id: Optional[str] = None
    ok: bool = False
    code: str = ""
    message: str = ""
    state_version: int = 0
    effects_applied: List[Effect] = Field(default_factory=list)
    events_fired: List[EventExecution] = Field(default_factory=list)
    quests: Dict[str, str] = Field(default_factory=dict)
    #: Storyline nodes entered by this action (empty in free mode).
    story_nodes_entered: List[str] = Field(default_factory=list)
    #: Knowledge entry ids the narrator was given (debug/audit only).
    context_entries: List[str] = Field(default_factory=list)
    #: Deterministic combat resolution for this request (None outside combat).
    combat_round: Optional[Dict[str, Any]] = None
    narrative: NarrativeResult = Field(default_factory=NarrativeResult)
    state: Optional[GameState] = None