"""Deterministic rules for the supported actions.

Every rule answers one question - may this action happen right now? - and, when
the answer is yes, which effects the world should record. Rules never mutate
state, never call an LLM and never execute configuration as code, so the same
inputs always produce the same decision.
"""
from __future__ import annotations

import hashlib
import random
from typing import Dict, List, Optional, Tuple

from app.engines.action_registry import get_action
from app.engines.event_engine import condition_holds, entity_item_count, entity_location
from app.models.game import (
    CombatantState,
    Condition,
    DoorDef,
    Effect,
    EntityDef,
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
        # The catalogue, not this method, decides which verbs exist: adding an
        # action means registering an ActionSpec, never editing a branch chain.
        spec = get_action(kind)
        handler = getattr(self, spec.handler, None) if spec is not None else None
        if spec is None or handler is None:
            return RuleOutcome(
                ok=False,
                code="action_not_supported",
                message=f"The action '{kind}' is not supported by the rules engine.",
            )
        outcome = handler(world, state, target_id, params)
        if outcome.ok:
            changed = [
                entity
                for entity in self._entities_present(world, state)
                if entity.id not in state.entities
            ]
            if changed:
                # The world says where entities *start*; the campaign state says
                # where they are now. Materialise who is here first, so their
                # position and health become authoritative (and survive reload)
                # from the first turn of the campaign onwards.
                seeding = [
                    Effect(kind="seed_entity", target=entity.id) for entity in changed
                ]
                outcome = outcome.model_copy(
                    update={"effects": seeding + list(outcome.effects)}
                )
        return outcome

    # -- world-authored actions -------------------------------------------

    def _custom(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        """Decide an action the world declared, using the event engine's checks.

        The world supplies conditions and effects as data; this method is the
        only code involved, so a configuration can never add behaviour.
        """
        params = params or {}
        action_id = str(params.get("action_id") or "").strip()
        if not action_id:
            return RuleOutcome(
                ok=False,
                code="custom_action_missing",
                message=(
                    "A custom action needs params.action_id naming the world's "
                    "custom action."
                ),
            )
        spec = next(
            (item for item in getattr(world, "custom_actions", []) or [] if item.id == action_id),
            None,
        )
        if spec is None:
            return RuleOutcome(
                ok=False,
                code="unknown_custom_action",
                message=f"This world declares no custom action '{action_id}'.",
            )
        label = spec.label or spec.id
        if target_id and spec.target != "none" and not self._known_target(world, state, target_id):
            return RuleOutcome(
                ok=False,
                code="unknown_target",
                message=f"'{target_id}' is not something {label} can act on here.",
            )
        if spec.target == "required" and not target_id:
            return RuleOutcome(
                ok=False,
                code="custom_target_required",
                message=f"{label} needs a target.",
            )
        # Imported lazily so the rules module stays free of an import cycle.
        unmet = [condition for condition in spec.requires if not condition_holds(state, world, condition)]
        if unmet:
            reasons = "; ".join(self._describe_condition(condition) for condition in unmet)
            return RuleOutcome(
                ok=False,
                code="condition_failed",
                message=f"{label} is not possible right now: {reasons}.",
            )
        return RuleOutcome(
            ok=True,
            code="custom_action",
            message=spec.description or label,
            effects=list(spec.effects),
        )

    @staticmethod
    def _known_target(world: WorldDefinition, state: GameState, target_id: str) -> bool:
        if any(item.id == target_id for item in world.items):
            return True
        if any(door.id == target_id for door in world.doors):
            return True
        if any(entity.id == target_id for entity in world.entities):
            return True
        if any(location.id == target_id for location in world.locations):
            return True
        return target_id in state.inventory

    @staticmethod
    def _describe_condition(condition: Condition) -> str:
        kind = condition.kind
        if kind == "has_item":
            return f"you need '{condition.target}'"
        if kind == "item_count":
            return f"you need {condition.value} x '{condition.target}'"
        if kind == "door_open":
            wanted = "open" if condition.value in (None, True) else "closed"
            return f"door '{condition.target}' must be {wanted}"
        if kind == "at_location":
            return f"you must be at '{condition.target}'"
        if kind == "quest_status":
            return f"quest '{condition.target}' must be {condition.value}"
        if kind == "flag":
            return f"'{condition.target}' must be {condition.value}"
        if kind == "combatant_hp":
            return f"'{condition.target}' health must be {condition.op} {condition.value}"
        if kind == "combatant_alive":
            wanted = "alive" if condition.value in (None, True) else "dead"
            return f"'{condition.target}' must be {wanted}"
        if kind == "entity_at":
            return f"'{condition.target}' must be at '{condition.value}'"
        if kind == "entity_alive":
            wanted = "alive" if condition.value in (None, True) else "dead"
            return f"'{condition.target}' must be {wanted}"
        return f"{kind} '{condition.target}'"

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

    @classmethod
    def _entities_present(cls, world: WorldDefinition, state: GameState) -> List[EntityDef]:
        """Living people and creatures at their current runtime location."""
        return [
            entity
            for entity in world.entities
            if cls._entity_here(state, world, entity) and entity.kind != "object"
            and cls._entity_alive(state, world, entity.id)
        ]

    @staticmethod
    def _entity_here(state: GameState, world: WorldDefinition, entity: EntityDef) -> bool:
        return entity_location(state, world, entity.id) == state.location_id

    @staticmethod
    def _entity_alive(state: GameState, world: WorldDefinition, entity_id: str) -> bool:
        combatant = state.combatants.get(entity_id)
        if combatant is not None and not combatant.alive:
            return False
        record = state.entities.get(entity_id)
        return record.alive if record is not None else True

    @staticmethod
    def _door_is_open(state: GameState, door: DoorDef) -> bool:
        return state.doors.get(door.id, door.opened)

    # -- actions -----------------------------------------------------------

    def _look(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if target_id:
            return RuleOutcome(
                ok=False,
                code="unsupported_target",
                message="look describes the current location; inspecting a target is not supported.",
            )
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
        people = [entity.name for entity in self._entities_present(world, state)]
        containers = []
        for entity in world.entities:
            if entity.kind != "object":
                continue
            if entity_location(state, world, entity.id) != state.location_id:
                continue
            contents = [
                items[item_id].name
                for item_id in entity.items
                if item_id in items
                and entity_item_count(state, world, entity.id, item_id) > 0
            ]
            if contents:
                containers.append(f"{entity.name}: " + ", ".join(contents))
        parts = [loc.description or loc.name]
        if here:
            parts.append("You can see: " + ", ".join(here) + ".")
        if door_names:
            parts.append("Doors: " + ", ".join(door_names) + ".")
        if people:
            parts.append("People here: " + ", ".join(people) + ".")
        if containers:
            parts.append("Containers here: " + "; ".join(containers) + ".")
        if exits:
            parts.append("Exits: " + ", ".join(exits) + ".")
        return RuleOutcome(
            ok=True,
            code="looked",
            message=" ".join(parts),
        )

    def _investigate(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if not target_id:
            return self._look(world, state, None, params)
        locations = self._locations(world)
        items = self._items(world)
        doors = self._doors(world)
        entities = {entity.id: entity for entity in world.entities}

        entity = entities.get(target_id)
        if entity is not None:
            if entity_location(state, world, entity.id) != state.location_id:
                return RuleOutcome(
                    ok=False, code="entity_not_here",
                    message=f"{entity.name} is not here.",
                )
            parts = [f"{entity.name}: {entity.description or entity.kind}."]
            record = state.entities.get(entity.id)
            if record is not None and entity.kind != "object":
                parts.append(f"HP {record.hp}/{record.max_hp}.")
            contents = [
                items[item_id].name
                for item_id in entity.items
                if item_id in items
                and entity_item_count(state, world, entity.id, item_id) > 0
            ]
            if contents:
                parts.append("It holds: " + ", ".join(contents) + ".")
            if entity.dialogue and entity.kind != "object":
                parts.append("It seems willing to talk.")
            return RuleOutcome(ok=True, code="investigated", message=" ".join(parts))

        item = items.get(target_id)
        if item is not None:
            held = state.inventory.get(target_id, 0) > 0
            on_ground = state.item_locations.get(target_id) == state.location_id
            in_container = any(
                entity_location(state, world, candidate.id) == state.location_id
                and entity_item_count(state, world, candidate.id, target_id) > 0
                for candidate in world.entities
            )
            if not (held or on_ground or in_container):
                return RuleOutcome(
                    ok=False, code="item_not_here",
                    message=f"The {item.name} is not here.",
                )
            where = (
                "you carry it" if held
                else "it is on the ground" if on_ground
                else "it is inside something here"
            )
            return RuleOutcome(
                ok=True, code="investigated",
                message=f"{item.name}: {item.description or 'no notable detail'} ({where}).",
            )

        door = doors.get(target_id)
        if door is not None:
            if door.location_id != state.location_id:
                return RuleOutcome(
                    ok=False, code="door_not_here",
                    message=f"The {door.name} is not here.",
                )
            is_open = self._door_is_open(state, door)
            detail = "open" if is_open else ("locked" if door.locked else "closed")
            key = ""
            if door.key_item_id and door.key_item_id in items:
                key = f" It needs the {items[door.key_item_id].name}."
            return RuleOutcome(
                ok=True, code="investigated",
                message=f"{door.name}: {detail}.{key}",
            )

        location = locations.get(target_id)
        if location is not None:
            if target_id != state.location_id and not any(
                exit_def.target_id == target_id
                for exit_def in (locations.get(state.location_id).exits if state.location_id in locations else [])
            ):
                return RuleOutcome(
                    ok=False, code="location_not_here",
                    message=f"{location.name} is not within view.",
                )
            return RuleOutcome(
                ok=True, code="investigated",
                message=f"{location.name}: {location.description or 'no description'}",
            )

        return RuleOutcome(
            ok=False, code="unknown_target",
            message=f"There is nothing called '{target_id}' to investigate.",
        )

    def _exit_problem(
        self,
        world: WorldDefinition,
        state: GameState,
        locations: Dict[str, LocationDef],
        doors: Dict[str, DoorDef],
        items: Dict[str, ItemDef],
        source_id: str,
        destination_id: str,
    ) -> Optional[RuleOutcome]:
        source = locations.get(source_id)
        destination = locations.get(destination_id)
        if source is None or destination is None:
            return RuleOutcome(
                ok=False, code="unknown_location",
                message="The route names an undefined location.",
            )
        exit_def = next(
            (e for e in source.exits if e.target_id == destination_id), None
        )
        if exit_def is None:
            return RuleOutcome(
                ok=False, code="no_route",
                message=f"You cannot reach {destination.name} from {source.name}.",
            )
        if exit_def.requires_item_id:
            if state.inventory.get(exit_def.requires_item_id, 0) <= 0:
                item = items.get(exit_def.requires_item_id)
                name = item.name if item else exit_def.requires_item_id
                return RuleOutcome(
                    ok=False, code="missing_item",
                    message=f"You need the {name} to go that way.",
                )
        if exit_def.door_id:
            door = doors.get(exit_def.door_id)
            if door is None:
                return RuleOutcome(
                    ok=False, code="unknown_door",
                    message=f"The way is blocked by an undefined door '{exit_def.door_id}'.",
                )
            if not self._door_is_open(state, door):
                return RuleOutcome(
                    ok=False, code="exit_blocked",
                    message=f"The {door.name} is closed.",
                )
        return None

    def _move(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if state.combat.get("active") or state.flags.get("combat_active"):
            return RuleOutcome(
                ok=False,
                code="combat_active",
                message="You cannot move during combat; use flee to escape first.",
            )
        if not target_id:
            return RuleOutcome(
                ok=False,
                code="missing_target",
                message="move requires a target_id (the destination location id).",
            )
        locations = self._locations(world)
        doors = self._doors(world)
        items = self._items(world)
        if state.location_id not in locations:
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

        raw_path = (params or {}).get("path")
        path: List[str] = []
        if isinstance(raw_path, list) and raw_path:
            if not all(isinstance(step, str) and step for step in raw_path):
                return RuleOutcome(
                    ok=False, code="invalid_path",
                    message="path must be a non-empty list of location ids.",
                )
            if raw_path[-1] != target_id:
                return RuleOutcome(
                    ok=False, code="invalid_path",
                    message="the path must end at the destination.",
                )
            if len(raw_path) > len(locations):
                return RuleOutcome(
                    ok=False, code="invalid_path",
                    message="the path is longer than the world has locations.",
                )
            path = list(raw_path)
        else:
            path = [target_id]

        cursor = state.location_id
        for step in path:
            problem = self._exit_problem(
                world, state, locations, doors, items, cursor, step
            )
            if problem is not None:
                return problem
            cursor = step

        if len(path) > 1:
            message = f"You travel to {destination.name}."
        else:
            message = f"You move to {destination.name}."
        return RuleOutcome(
            ok=True,
            code="moved",
            message=message,
            effects=[Effect(kind="move_player", target=target_id)],
        )

    def _equip(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if not target_id:
            return RuleOutcome(
                ok=False, code="missing_target", message="equip requires an item id."
            )
        item = self._items(world).get(target_id)
        if item is None:
            return RuleOutcome(
                ok=False, code="unknown_item", message=f"There is no item '{target_id}'."
            )
        if not item.slot:
            return RuleOutcome(
                ok=False, code="not_equippable",
                message=f"The {item.name} cannot be equipped.",
            )
        if state.inventory.get(target_id, 0) <= 0:
            return RuleOutcome(
                ok=False, code="not_carried",
                message=f"You do not carry the {item.name}.",
            )
        return RuleOutcome(
            ok=True, code="equipped",
            message=f"You equip the {item.name}.",
            effects=[Effect(kind="equip_item", target=target_id)],
        )

    def _unequip(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        items = self._items(world)
        slot = target_id if target_id in state.equipment else ""
        if not slot and target_id:
            slot = next(
                (name for name, item_id in state.equipment.items() if item_id == target_id),
                "",
            )
        if not slot and not target_id and len(state.equipment) == 1:
            slot = next(iter(state.equipment))
        if not slot:
            return RuleOutcome(
                ok=False, code="not_equipped",
                message="That item is not equipped.",
            )
        item_id = state.equipment.get(slot, "")
        name = items[item_id].name if item_id in items else item_id
        return RuleOutcome(
            ok=True, code="unequipped",
            message=f"You remove the {name}.",
            effects=[Effect(kind="unequip_item", target=slot)],
        )

    def _merchant_here(
        self, world: WorldDefinition, state: GameState, merchant_id: str = ""
    ):
        merchants = [
            entity
            for entity in self._entities_present(world, state)
            if entity.shop_items
        ]
        if merchant_id:
            return next(
                (entity for entity in merchants if entity.id == merchant_id),
                None,
            )
        return merchants[0] if merchants else None

    def _buy(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if not target_id:
            return RuleOutcome(
                ok=False, code="missing_target", message="buy requires an item id."
            )
        item = self._items(world).get(target_id)
        if item is None:
            return RuleOutcome(
                ok=False, code="unknown_item", message=f"There is no item '{target_id}'."
            )
        merchant_id = str((params or {}).get("merchant_id") or "")
        merchant = self._merchant_here(world, state, merchant_id)
        if merchant is None:
            return RuleOutcome(
                ok=False, code="merchant_absent",
                message="That merchant is not here.",
            )
        if target_id not in merchant.shop_items:
            return RuleOutcome(
                ok=False, code="not_for_sale",
                message=f"The {merchant.name} does not sell the {item.name}.",
            )
        if item.price <= 0:
            return RuleOutcome(
                ok=False, code="not_for_sale",
                message=f"The {item.name} has no price and cannot be bought.",
            )
        if state.currency < item.price:
            return RuleOutcome(
                ok=False, code="insufficient_funds",
                message=f"The {item.name} costs {item.price}; you have {state.currency}.",
            )
        return RuleOutcome(
            ok=True, code="bought",
            message=f"You buy the {item.name} for {item.price}.",
            effects=[
                Effect(kind="remove_currency", amount=item.price),
                Effect(kind="give_item", target=item.id, amount=1, keep_ground=True),
            ],
        )

    def _sell(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if not target_id:
            return RuleOutcome(
                ok=False, code="missing_target", message="sell requires an item id."
            )
        item = self._items(world).get(target_id)
        if item is None:
            return RuleOutcome(
                ok=False, code="unknown_item", message=f"There is no item '{target_id}'."
            )
        if state.inventory.get(target_id, 0) <= 0:
            return RuleOutcome(
                ok=False, code="not_carried",
                message=f"You do not carry the {item.name}.",
            )
        merchant = self._merchant_here(world, state)
        if merchant is None:
            return RuleOutcome(
                ok=False, code="no_merchant",
                message="There is no merchant here to buy it.",
            )
        if item.price <= 0:
            return RuleOutcome(
                ok=False, code="no_value",
                message=f"The {item.name} has no trade value.",
            )
        offer = max(1, item.price // 2)
        return RuleOutcome(
            ok=True, code="sold",
            message=f"You sell the {item.name} for {offer}.",
            effects=[
                Effect(kind="remove_item", target=item.id, amount=1),
                Effect(kind="add_currency", amount=offer),
            ],
        )

    def _choose(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        pending = state.pending_choice
        if pending is None:
            return RuleOutcome(
                ok=False, code="no_pending_choice",
                message="There is no choice waiting to be resolved.",
            )
        option_ids = {option.get("id") for option in pending.options}
        if not target_id or target_id not in option_ids:
            return RuleOutcome(
                ok=False, code="invalid_choice",
                message="That option is not part of the pending choice.",
            )
        return RuleOutcome(
            ok=True, code="chosen",
            message="You make your choice.",
            effects=[
                Effect(kind="resolve_choice", target=pending.choice_id, value=target_id)
            ],
        )

    def _take(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
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
        owner_id: Optional[str] = None
        for entity in world.entities:
            if entity_location(state, world, entity.id) != state.location_id:
                continue
            if entity_item_count(state, world, entity.id, target_id) > 0:
                owner_id = entity.id
                break
        if state.item_locations.get(target_id) != state.location_id and owner_id is None:
            return RuleOutcome(
                ok=False,
                code="item_not_here",
                message=f"The {item.name} is not here.",
            )
        if item.pickup_requires:
            unmet = [
                condition
                for condition in item.pickup_requires
                if not condition_holds(state, world, condition)
            ]
            if unmet:
                reasons = "; ".join(
                    self._describe_condition(condition) for condition in unmet
                )
                return RuleOutcome(
                    ok=False,
                    code="pickup_refused",
                    message=f"You cannot pick up the {item.name} yet: {reasons}.",
                )
        if owner_id is not None:
            return RuleOutcome(
                ok=True,
                code="taken",
                message=f"You take the {item.name}.",
                effects=[
                    Effect(
                        kind="take_entity",
                        target=owner_id,
                        value=target_id,
                        amount=1,
                    )
                ],
            )
        return RuleOutcome(
            ok=True,
            code="taken",
            message=f"You pick up the {item.name}.",
            effects=[Effect(kind="give_item", target=target_id, amount=1)],
        )

    def _open(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
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
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
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
        # Authored behaviour wins: the world decides what using this item does.
        if item.use_effects:
            effects: List[Effect] = list(item.use_effects)
            if item.consumable:
                effects.append(Effect(kind="remove_item", target=target_id, amount=1))
            return RuleOutcome(
                ok=True,
                code="used_item",
                message=f"You use the {item.name}.",
                effects=effects,
            )
        # Otherwise the built-in behaviour: a key standing at its own door.
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
        door_effects: List[Effect] = [Effect(kind="open_door", target=door.id)]
        if door.consumes_key:
            door_effects.append(
                Effect(kind="remove_item", target=target_id, amount=1)
            )
        return RuleOutcome(
            ok=True,
            code="used_item",
            message=f"You use the {item.name} to open the {door.name}.",
            effects=door_effects,
        )

    # -- combat -------------------------------------------------------------

    @staticmethod
    def _seed(state: GameState, action: str, target_id: Optional[str]) -> int:
        """Deterministic RNG seed for one combat turn.

        Combat is resolved as pure arithmetic: the same campaign, the same
        target and the same turn always produce the same rolls, so a replay,
        a snapshot or a test can never disagree with what was committed.
        """
        raw = f"{state.campaign_id}|{state.turn}|{action}|{target_id or ''}"
        return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16], 16)

    @staticmethod
    def _num(entity: EntityDef, key: str, default: float) -> float:
        value = entity.attributes.get(key, default)
        if isinstance(value, bool):
            return float(default)
        if isinstance(value, (int, float)):
            return float(value)
        return float(default)

    @classmethod
    def combatant_for(
        cls, entity: EntityDef, state: GameState
    ) -> CombatantState:
        """Current vitals for an entity, seeded from its authored attributes."""
        existing = state.combatants.get(entity.id)
        if existing is not None:
            return existing
        max_hp = max(1, int(cls._num(entity, "max_hp", cls._num(entity, "hp", 10))))
        return CombatantState(
            entity_id=entity.id,
            name=entity.name,
            hp=max_hp,
            max_hp=max_hp,
            alive=True,
            attack=max(1, int(cls._num(entity, "attack", 3))),
            defense=max(0, int(cls._num(entity, "defense", 1))),
            flee_chance=min(1.0, max(0.0, cls._num(entity, "flee_chance", 0.5))),
        )

    @staticmethod
    def _player_stats(world: WorldDefinition, state: GameState) -> Tuple[int, int]:
        attack = int(state.flags.get("player_attack", 4) or 4)
        defense = int(state.flags.get("player_defense", 1) or 1)
        items = {item.id: item for item in world.items}
        for item_id in state.equipment.values():
            item = items.get(item_id)
            if item is None:
                continue
            attack += int(item.bonus.get("attack", 0) or 0)
            defense += int(item.bonus.get("defense", 0) or 0)
        return attack, defense

    @staticmethod
    def _player_alive(state: GameState) -> bool:
        if state.flags.get("player_defeated"):
            return False
        hp = state.flags.get("player_hp")
        return not (isinstance(hp, int) and hp <= 0)

    def _enemy(
        self, world: WorldDefinition, state: GameState, target_id: Optional[str]
    ) -> Tuple[Optional[EntityDef], Optional[CombatantState], Optional[RuleOutcome]]:
        entities = {entity.id: entity for entity in world.entities}
        if not target_id:
            current = state.combat.get("target_id")
            entity = entities.get(current) if current else None
            if entity is None:
                return None, None, RuleOutcome(
                    ok=False,
                    code="missing_target",
                    message="attack requires a target_id (the entity id).",
                )
        else:
            entity = entities.get(target_id)
            if entity is None:
                return None, None, RuleOutcome(
                    ok=False,
                    code="unknown_target",
                    message=f"There is no creature called '{target_id}' here.",
                )
        if entity.kind == "object":
            return None, None, RuleOutcome(
                ok=False,
                code="unsupported_target_kind",
                message=f"{entity.name} is not a combatant.",
            )
        if not self._entity_here(state, world, entity):
            return None, None, RuleOutcome(
                ok=False,
                code="entity_not_here",
                message=f"{entity.name} is not here.",
            )
        combatant = self.combatant_for(entity, state)
        if not self._entity_alive(state, world, entity.id):
            return entity, combatant, RuleOutcome(
                ok=False,
                code="target_defeated",
                message=f"{entity.name} is already defeated.",
            )
        return entity, combatant, None

    def _attack(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str],
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if not self._player_alive(state):
            return RuleOutcome(
                ok=False,
                code="player_defeated",
                message="You are in no shape to fight any more.",
            )
        entity, combatant, error = self._enemy(world, state, target_id)
        if error is not None:
            return error
        assert entity is not None and combatant is not None
        rng = random.Random(self._seed(state, "attack", entity.id))
        player_attack, _player_defense = self._player_stats(world, state)
        # A hit needs more than a lucky swing: compare the attacker's power to
        # the target's guard and fold the remainder into a bounded chance.
        hit_chance = min(0.95, max(0.05, 0.5 + (player_attack - combatant.defense) * 0.1))
        hit = rng.random() < hit_chance
        damage = 0
        if hit:
            base = max(1, player_attack - combatant.defense // 2)
            damage = max(1, base + rng.randint(0, max(0, base // 2)))
            damage = min(damage, combatant.hp)
        remaining = combatant.hp - damage
        defeated = hit and remaining <= 0
        counter = 0
        if not defeated:
            enemy_hit_chance = min(0.9, max(0.05, 0.5 + (combatant.attack - _player_defense) * 0.1))
            if rng.random() < enemy_hit_chance:
                counter = max(1, combatant.attack - _player_defense // 2 + rng.randint(0, 1))
        effects: List[Effect] = [
            Effect(kind="combat_hp", target=entity.id, value=remaining),
            Effect(kind="set_flag", target="combat_target", value=entity.id),
            Effect(kind="set_flag", target="combat_round", value=int(state.turn) + 1),
        ]
        if defeated:
            effects.append(Effect(kind="emit", target="combat_enemy_defeated", value=entity.id))
            effects.append(Effect(kind="set_flag", target="combat_active", value=False))
        else:
            effects.append(Effect(kind="set_flag", target="combat_active", value=True))
        if counter:
            effects.append(Effect(kind="player_damage", target="player", amount=counter))
        effects.append(Effect(kind="adjust_alert", target=entity.id, amount=1))
        if hit:
            effects.append(
                Effect(kind="adjust_relation", target=entity.id, value="player", amount=-1)
            )
        parts = [f"You strike at {entity.name}." if hit else f"You lunge at {entity.name} and miss."]
        if hit:
            parts.append(f"{entity.name} takes {damage} damage.")
        if defeated:
            parts.append(f"{entity.name} is defeated.")
        elif counter:
            parts.append(f"{entity.name} hits back for {counter} damage.")
        else:
            parts.append(f"{entity.name} fails to land a blow.")
        return RuleOutcome(
            ok=True,
            code="enemy_defeated" if defeated else ("player_hit" if hit else "player_missed"),
            message=" ".join(parts),
            effects=effects,
        )

    def _defend(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str],
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if not self._player_alive(state):
            return RuleOutcome(
                ok=False,
                code="player_defeated",
                message="You are in no shape to fight any more.",
            )
        entity, combatant, error = self._enemy(world, state, target_id)
        if error is not None:
            if error.code in ("missing_target", "unknown_target", "entity_not_here"):
                return RuleOutcome(
                    ok=False,
                    code="not_in_combat",
                    message="There is nothing to defend against here.",
                )
            return error
        assert entity is not None and combatant is not None
        rng = random.Random(self._seed(state, "defend", entity.id))
        _attack, player_defense = self._player_stats(world, state)
        guard = 2 + player_defense
        hit_chance = min(0.9, max(0.05, 0.5 + (combatant.attack - (player_defense + 2)) * 0.1))
        damage = 0
        if rng.random() < hit_chance:
            damage = max(1, combatant.attack - (player_defense + 2) // 2 + rng.randint(0, 1))
        effects: List[Effect] = [
            Effect(kind="combat_hp", target=entity.id, value=combatant.hp),
            Effect(kind="set_flag", target="combat_target", value=entity.id),
            Effect(kind="set_flag", target="combat_active", value=True),
        ]
        if damage:
            effects.append(Effect(kind="player_damage", target="player", amount=damage))
            message = (
                f"You raise your guard (+{guard}). {entity.name} breaks through "
                f"for {damage} damage."
            )
        else:
            message = f"You raise your guard (+{guard}). {entity.name} cannot get past it."
        return RuleOutcome(
            ok=True, code="defended", message=message, effects=effects
        )

    def _flee(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if not self._player_alive(state):
            return RuleOutcome(
                ok=False,
                code="player_defeated",
                message="You are in no shape to run any more.",
            )
        target = state.combat.get("target_id") or state.flags.get("combat_target")
        entity, combatant, error = self._enemy(world, state, target)
        if error is not None:
            if error.code in ("missing_target", "unknown_target", "entity_not_here"):
                return RuleOutcome(
                    ok=False,
                    code="not_in_combat",
                    message="You are not in combat with anyone here.",
                )
            return error
        assert entity is not None and combatant is not None
        rng = random.Random(self._seed(state, "flee", entity.id))
        if rng.random() < combatant.flee_chance:
            return RuleOutcome(
                ok=True,
                code="escaped",
                message=f"You break away from {entity.name}.",
                effects=[
                    Effect(kind="set_flag", target="combat_active", value=False),
                    Effect(kind="emit", target="combat_escaped", value=entity.id),
                ],
            )
        damage = max(1, combatant.attack // 2)
        return RuleOutcome(
            ok=True,
            code="escape_failed",
            message=f"You try to flee, but {entity.name} blocks you for {damage} damage.",
            effects=[
                Effect(kind="set_flag", target="combat_active", value=True),
                Effect(kind="player_damage", target="player", amount=damage),
            ],
        )

    def _talk(
        self,
        world: WorldDefinition,
        state: GameState,
        target_id: Optional[str] = None,
        params: Optional[Dict] = None,
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
        present = self._entities_present(world, state)
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
            if not self._entity_here(state, world, entity):
                return RuleOutcome(
                    ok=False,
                    code="entity_not_here",
                    message=f"{entity.name} is not here.",
                )
            if entity.kind == "object":
                return RuleOutcome(
                    ok=False,
                    code="unsupported_target_kind",
                    message=f"{entity.name} cannot talk.",
                )
            if not self._entity_alive(state, world, entity.id):
                return RuleOutcome(
                    ok=False,
                    code="entity_dead",
                    message=f"{entity.name} is beyond conversation.",
                )
            record = state.entities.get(entity.id)
            index = 0
            if record is not None:
                stored = record.flags.get("dialogue_index", 0)
                index = int(stored) if isinstance(stored, (int, float)) else 0
            line = (
                entity.dialogue[index % len(entity.dialogue)]
                if entity.dialogue
                else ""
            )
            message = (
                f'{entity.name}: "{line}"'
                if line
                else f"You talk to {entity.name}."
            )
            effects = (
                [Effect(kind="advance_dialogue", target=entity.id)]
                if entity.dialogue
                else []
            )
            return RuleOutcome(
                ok=True, code="talked", message=message, effects=effects
            )
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
