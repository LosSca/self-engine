"""Structured edits to an authored world document.

The editor never patches JSON in place: each helper works on a deep copy, asks
the same validators the engine uses at run time, and only then hands the new
document back to the caller. A rejected edit therefore leaves the stored world
exactly as it was.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, TypeVar

from app.models.game import (
    CustomActionDef,
    DoorDef,
    EntityDef,
    GameEventDef,
    ItemDef,
    LocationDef,
    QuestDef,
    WorldDefinition,
)
from app.services.world_service import WorldValidationError

T = TypeVar("T")


def _replace_or_append(items: List[T], value: T, item_id: str) -> None:
    for index, existing in enumerate(items):
        if getattr(existing, "id", None) == item_id:
            items[index] = value
            return
    items.append(value)


def _require_id(kind: str, item_id: str, expected: Optional[str]) -> None:
    if expected is not None and item_id != expected:
        raise WorldValidationError(
            [f"{kind} id '{item_id}' does not match the path id '{expected}'"]
        )


def _finish(world: WorldDefinition) -> WorldDefinition:
    errors = world.validate_references()
    if errors:
        raise WorldValidationError(errors)
    return world


def _copy(world: WorldDefinition) -> WorldDefinition:
    return world.model_copy(deep=True)


def upsert_location(
    world: WorldDefinition, location: LocationDef, location_id: Optional[str] = None
) -> WorldDefinition:
    _require_id("location", location.id, location_id)
    draft = _copy(world)
    _replace_or_append(draft.locations, location, location.id)
    return _finish(draft)


def delete_location(world: WorldDefinition, location_id: str) -> WorldDefinition:
    draft = _copy(world)
    remaining = [loc for loc in draft.locations if loc.id != location_id]
    if len(remaining) == len(draft.locations):
        raise LookupError(f"unknown location '{location_id}'")
    draft.locations = remaining
    return _finish(draft)


def upsert_item(
    world: WorldDefinition, item: ItemDef, item_id: Optional[str] = None
) -> WorldDefinition:
    _require_id("item", item.id, item_id)
    draft = _copy(world)
    _replace_or_append(draft.items, item, item.id)
    return _finish(draft)


def delete_item(world: WorldDefinition, item_id: str) -> WorldDefinition:
    draft = _copy(world)
    remaining = [item for item in draft.items if item.id != item_id]
    if len(remaining) == len(draft.items):
        raise LookupError(f"unknown item '{item_id}'")
    draft.items = remaining
    for location in draft.locations:
        location.item_ids = [i for i in location.item_ids if i != item_id]
    return _finish(draft)


def upsert_door(
    world: WorldDefinition, door: DoorDef, door_id: Optional[str] = None
) -> WorldDefinition:
    _require_id("door", door.id, door_id)
    draft = _copy(world)
    _replace_or_append(draft.doors, door, door.id)
    return _finish(draft)


def delete_door(world: WorldDefinition, door_id: str) -> WorldDefinition:
    draft = _copy(world)
    remaining = [door for door in draft.doors if door.id != door_id]
    if len(remaining) == len(draft.doors):
        raise LookupError(f"unknown door '{door_id}'")
    draft.doors = remaining
    for location in draft.locations:
        location.door_ids = [d for d in location.door_ids if d != door_id]
        for exit_def in location.exits:
            if exit_def.door_id == door_id:
                exit_def.door_id = None
    return _finish(draft)


def upsert_entity(
    world: WorldDefinition, entity: EntityDef, entity_id: Optional[str] = None
) -> WorldDefinition:
    _require_id("entity", entity.id, entity_id)
    draft = _copy(world)
    _replace_or_append(draft.entities, entity, entity.id)
    # Keep the legacy per-location display list in sync for the common case.
    for location in draft.locations:
        names = [npc for npc in location.npc_ids if npc != entity.id]
        if location.id == entity.location_id:
            names.append(entity.id)
        location.npc_ids = names
    return _finish(draft)


def delete_entity(world: WorldDefinition, entity_id: str) -> WorldDefinition:
    draft = _copy(world)
    remaining = [entity for entity in draft.entities if entity.id != entity_id]
    if len(remaining) == len(draft.entities):
        raise LookupError(f"unknown entity '{entity_id}'")
    draft.entities = remaining
    for location in draft.locations:
        location.npc_ids = [npc for npc in location.npc_ids if npc != entity_id]
    return _finish(draft)


def upsert_event(
    world: WorldDefinition, event: GameEventDef, event_id: Optional[str] = None
) -> WorldDefinition:
    _require_id("event", event.id, event_id)
    draft = _copy(world)
    _replace_or_append(draft.events, event, event.id)
    _finish(draft)
    from app.engines.event_engine import validate_event

    errors = validate_event(draft, event)
    if errors:
        raise WorldValidationError(errors)
    return draft


def delete_event(world: WorldDefinition, event_id: str) -> WorldDefinition:
    draft = _copy(world)
    remaining = [event for event in draft.events if event.id != event_id]
    if len(remaining) == len(draft.events):
        raise LookupError(f"unknown event '{event_id}'")
    draft.events = remaining
    return _finish(draft)


def upsert_quest(
    world: WorldDefinition, quest: QuestDef, quest_id: Optional[str] = None
) -> WorldDefinition:
    _require_id("quest", quest.id, quest_id)
    draft = _copy(world)
    _replace_or_append(draft.quests, quest, quest.id)
    return _finish(draft)


def delete_quest(world: WorldDefinition, quest_id: str) -> WorldDefinition:
    draft = _copy(world)
    remaining = [quest for quest in draft.quests if quest.id != quest_id]
    if len(remaining) == len(draft.quests):
        raise LookupError(f"unknown quest '{quest_id}'")
    draft.quests = remaining
    return _finish(draft)


def upsert_custom_action(
    world: WorldDefinition,
    action: CustomActionDef,
    action_id: Optional[str] = None,
) -> WorldDefinition:
    _require_id("custom action", action.id, action_id)
    draft = _copy(world)
    _replace_or_append(draft.custom_actions, action, action.id)
    return _finish(draft)


def delete_custom_action(world: WorldDefinition, action_id: str) -> WorldDefinition:
    draft = _copy(world)
    remaining = [item for item in draft.custom_actions if item.id != action_id]
    if len(remaining) == len(draft.custom_actions):
        raise LookupError(f"unknown custom action '{action_id}'")
    draft.custom_actions = remaining
    return _finish(draft)


def replace_world(world: WorldDefinition) -> WorldDefinition:
    """Whole-document save used by the world editor's JSON tab."""
    return _finish(world.model_copy(deep=True))
