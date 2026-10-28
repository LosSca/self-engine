"""Deterministic rules for the supported actions.

Every rule answers one question - may this action happen right now? - and, when
the answer is yes, which effects the world should record. Rules never mutate
state, never call an LLM and never execute configuration as code, so the same
inputs always produce the same decision.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from app.models.game import (
    DoorDef,
    Effect,
    GameState,
    ItemDef,
    LocationDef,
    RuleOutcome,
    WorldDefinition,
)


class RulesEngine:
    """Stateless rule evaluator: (world, state, action) -> RuleOutcome."""

    def evaluate(
        self,
        world: WorldDefinition,
        state: GameState,
        action: str,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        params = params or {}
        kind = str(action)
        if kind == "look":
            return self._look(world, state)
        if kind == "move":
            return self._move(world, state, target_id)
        if kind == "take":
            return self._take(world, state, target_id)
        if kind == "open":
            return self._open(world, state, target_id)
        if kind == "use":
            return self._use(world, state, target_id)
        if kind == "talk":
            return self._talk(world, state, target_id)
        return RuleOutcome(
            ok=False,
            code="action_not_supported",
            message=f"The action '{kind}' is not supported by the rules engine.",
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _items(world: WorldDefinition) -> Dict[str, ItemDef]:
        return {item.id: item for item in world.items}

    @staticmethod
    def _doors(world: WorldDefinition) -> Dict[str, DoorDef]:
        return {door.id: door for door in world.doors}

    @staticmethod
    def _locations(world: WorldDefinition) -> Dict[str, LocationDef]:
        return {loc.id: loc for loc in world.locations}

    @staticmethod
    def _door_is_open(state: GameState, door: DoorDef) -> bool:
        return state.doors.get(door.id, door.opened)

    # -- actions -----------------------------------------------------------

    def _look(self, world: WorldDefinition, state: GameState) -> RuleOutcome:
        locations = self._locations(world)
        items = self._items(world)
        doors = self._doors(world)
        loc = locations.get(state.location_id)
        if loc is None:
            return RuleOutcome(
                ok=False,
                code="unknown_location",
                message=f"The current location '{state.location_id}' is not defined.",
            )
        here = [
            items[item_id].name
            for item_id in loc.item_ids
            if item_id in items and state.item_locations.get(item_id) == loc.id
        ]
        door_names = [
            f"{doors[door_id].name} ({'open' if self._door_is_open(state, doors[door_id]) else 'closed'})"
            for door_id in loc.door_ids
            if door_id in doors
        ]
        exits = [
            locations[exit_def.target_id].name
            for exit_def in loc.exits
            if exit_def.target_id in locations
        ]
        people = [
            entity.name
            for entity in world.entities
            if entity.location_id == loc.id and entity.kind != "object"
        ]
        parts = [loc.description or loc.name]
        if here:
            parts.append("You can see: " + ", ".join(here) + ".")
        if door_names:
            parts.append("Doors: " + ", ".join(door_names) + ".")
        if people:
            parts.append("People here: " + ", ".join(people) + ".")
        if exits:
            parts.append("Exits: " + ", ".join(exits) + ".")
        return RuleOutcome(
            ok=True,
            code="looked",
            message=" ".join(parts),
        )

    def _move(
        self, world: WorldDefinition, state: GameState, target_id: Optional[str]
    ) -> RuleOutcome:
        if not target_id:
            return RuleOutcome(
                ok=False,
                code="missing_target",
                message="move requires a target_id (the destination location id).",
            )
        locations = self._locations(world)
        doors = self._doors(world)
        items = self._items(world)
        loc = locations.get(state.location_id)
        if loc is None:
            return RuleOutcome(
                ok=False,
                code="unknown_location",
                message=f"The current location '{state.location_id}' is not defined.",
            )
        destination = locations.get(target_id)
        if destination is None:
            return RuleOutcome(
                ok=False,
                code="unknown_location",
                message=f"The destination '{target_id}' is not defined.",
            )
        exit_def = next((e for e in loc.exits if e.target_id == target_id), None)
        if exit_def is None:
            return RuleOutcome(
                ok=False,
                code="no_route",
                message=f"You cannot reach {destination.name} from {loc.name}.",
            )
        if exit_def.requires_item_id:
            if state.inventory.get(exit_def.requires_item_id, 0) <= 0:
                item = items.get(exit_def.requires_item_id)
                name = item.name if item else exit_def.requires_item_id
                return RuleOutcome(
                    ok=False,
                    code="missing_item",
                    message=f"You need the {name} to go that way.",
                )
        if exit_def.door_id:
            door = doors.get(exit_def.door_id)
            if door is None:
                return RuleOutcome(
                    ok=False,
                    code="unknown_door",
                    message=f"The way is blocked by an undefined door '{exit_def.door_id}'.",
                )
            if not self._door_is_open(state, door):
                return RuleOutcome(
                    ok=False,
                    code="exit_blocked",
                    message=f"The {door.name} is closed.",
                )
        return RuleOutcome(
            ok=True,
            code="moved",
            message=f"You move to {destination.name}.",
            effects=[Effect(kind="move_player", target=target_id)],
        )

    def _take(
        self, world: WorldDefinition, state: GameState, target_id: Optional[str]
    ) -> RuleOutcome:
        if not target_id:
            return RuleOutcome(
                ok=False,
                code="missing_target",
                message="take requires a target_id (the item id).",
            )
        items = self._items(world)
        item = items.get(target_id)
        if item is None:
            return RuleOutcome(
                ok=False,
                code="unknown_item",
                message=f"There is no item '{target_id}' in this world.",
            )
        if state.inventory.get(target_id, 0) > 0:
            return RuleOutcome(
                ok=False,
                code="already_carried",
                message=f"You are already carrying the {item.name}.",
            )
        if state.item_locations.get(target_id) != state.location_id:
            return RuleOutcome(
                ok=False,
                code="item_not_here",
                message=f"The {item.name} is not here.",
            )
        return RuleOutcome(
            ok=True,
            code="taken",
            message=f"You pick up the {item.name}.",
            effects=[Effect(kind="give_item", target=target_id, amount=1)],
        )

    def _open(
        self, world: WorldDefinition, state: GameState, target_id: Optional[str]
    ) -> RuleOutcome:
        if not target_id:
            return RuleOutcome(
                ok=False,
                code="missing_target",
                message="open requires a target_id (the door id).",
            )
        doors = self._doors(world)
        items = self._items(world)
        door = doors.get(target_id)
        if door is None:
            return RuleOutcome(
                ok=False,
                code="unknown_door",
                message=f"There is no door '{target_id}' here.",
            )
        if door.location_id != state.location_id:
            return RuleOutcome(
                ok=False,
                code="door_not_here",
                message=f"The {door.name} is not here.",
            )
        if self._door_is_open(state, door):
            return RuleOutcome(
                ok=False,
                code="door_already_open",
                message=f"The {door.name} is already open.",
            )
        if door.locked:
            key_id = door.key_item_id
            if not key_id:
                return RuleOutcome(
                    ok=False,
                    code="door_locked",
                    message=f"The {door.name} is locked and no key is configured for it.",
                )
            key = items.get(key_id)
            key_name = key.name if key else key_id
            if state.inventory.get(key_id, 0) <= 0:
                return RuleOutcome(
                    ok=False,
                    code="door_locked",
                    message=f"The {door.name} is locked. You need the {key_name}.",
                )
            effects: List[Effect] = [Effect(kind="open_door", target=door.id)]
            if door.consumes_key:
                effects.append(Effect(kind="remove_item", target=key_id, amount=1))
            return RuleOutcome(
                ok=True,
                code="door_unlocked",
                message=f"You unlock the {door.name} with the {key_name} and pull it open.",
                effects=effects,
            )
        return RuleOutcome(
            ok=True,
            code="door_opened",
            message=f"You open the {door.name}.",
            effects=[Effect(kind="open_door", target=door.id)],
        )

    def _use(
        self, world: WorldDefinition, state: GameState, target_id: Optional[str]
    ) -> RuleOutcome:
        if not target_id:
            return RuleOutcome(
                ok=False,
                code="missing_target",
                message="use requires a target_id (the item id).",
            )
        items = self._items(world)
        doors = self._doors(world)
        item = items.get(target_id)
        if item is None:
            return RuleOutcome(
                ok=False,
                code="unknown_item",
                message=f"There is no item '{target_id}' in this world.",
            )
        if state.inventory.get(target_id, 0) <= 0:
            return RuleOutcome(
                ok=False,
                code="item_not_carried",
                message=f"You are not carrying the {item.name}.",
            )
        usable_doors = [
            door
            for door in world.doors
            if door.key_item_id == target_id
            and door.location_id == state.location_id
            and not self._door_is_open(state, door)
        ]
        if not usable_doors:
            return RuleOutcome(
                ok=False,
                code="nothing_happens",
                message=f"You use the {item.name}, but nothing happens.",
            )
        door = usable_doors[0]
        effects: List[Effect] = [Effect(kind="open_door", target=door.id)]
        if door.consumes_key:
            effects.append(Effect(kind="remove_item", target=target_id, amount=1))
        return RuleOutcome(
            ok=True,
            code="used_item",
            message=f"You use the {item.name} to open the {door.name}.",
            effects=effects,
        )

    def _talk(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
    ) -> RuleOutcome:
        locations = self._locations(world)
        loc = locations.get(state.location_id)
        if loc is None:
            return RuleOutcome(
                ok=False,
                code="unknown_location",
                message=f"The current location '{state.location_id}' is not defined.",
            )
        entities = {entity.id: entity for entity in world.entities}
        present = [e for e in world.entities if e.location_id == loc.id]
        legacy_names = [npc for npc in loc.npc_ids if npc not in entities]
        if target_id:
            entity = entities.get(target_id)
            if entity is None:
                if target_id in loc.npc_ids:
                    return RuleOutcome(
                        ok=True,
                        code="talked",
                        message=f"You talk to {target_id}.",
                    )
                return RuleOutcome(
                    ok=False,
                    code="nobody_here",
                    message="There is nobody here by that name to talk to.",
                )
            if entity.location_id != loc.id:
                return RuleOutcome(
                    ok=False,
                    code="entity_not_here",
                    message=f"{entity.name} is not here.",
                )
            line = entity.dialogue[0] if entity.dialogue else ""
            message = (
                f'{entity.name}: "{line}"'
                if line
                else f"You talk to {entity.name}."
            )
            return RuleOutcome(ok=True, code="talked", message=message)
        names = [entity.name for entity in present] + legacy_names
        if not names:
            return RuleOutcome(
                ok=False,
                code="nobody_here",
                message="There is nobody here to talk to.",
            )
        return RuleOutcome(
            ok=True,
            code="talk_available",
            message="You can talk to: " + ", ".join(names) + ".",
        )


def describe_outcome(outcome: RuleOutcome) -> str:
    """Compact debug rendering used by traces and logs."""
    status = "ok" if outcome.ok else "rejected"
    return f"{status}:{outcome.code} - {outcome.message}"
