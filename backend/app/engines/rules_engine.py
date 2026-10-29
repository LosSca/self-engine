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

from app.models.game import (
    CombatantState,
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
        if kind == "attack":
            return self._attack(world, state, target_id, params)
        if kind == "defend":
            return self._defend(world, state, target_id, params)
        if kind == "flee":
            return self._flee(world, state, params)
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
    def _player_stats(state: GameState) -> Tuple[int, int]:
        attack = int(state.flags.get("player_attack", 4) or 4)
        defense = int(state.flags.get("player_defense", 1) or 1)
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
        if entity.location_id != state.location_id:
            return None, None, RuleOutcome(
                ok=False,
                code="entity_not_here",
                message=f"{entity.name} is not here.",
            )
        combatant = self.combatant_for(entity, state)
        if not combatant.alive:
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
        player_attack, _player_defense = self._player_stats(state)
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
        target = target_id or state.combat.get("target_id")
        entities = {entity.id: entity for entity in world.entities}
        entity = entities.get(target) if target else None
        if entity is None or entity.location_id != state.location_id:
            return RuleOutcome(
                ok=False,
                code="not_in_combat",
                message="There is nothing to defend against here.",
            )
        combatant = self.combatant_for(entity, state)
        if not combatant.alive:
            return RuleOutcome(
                ok=False,
                code="target_defeated",
                message=f"{entity.name} is already defeated.",
            )
        rng = random.Random(self._seed(state, "defend", entity.id))
        _attack, player_defense = self._player_stats(state)
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
        params: Optional[Dict] = None,
    ) -> RuleOutcome:
        if not self._player_alive(state):
            return RuleOutcome(
                ok=False,
                code="player_defeated",
                message="You are in no shape to run any more.",
            )
        target = state.flags.get("combat_target") or state.combat.get("target_id")
        entities = {entity.id: entity for entity in world.entities}
        entity = entities.get(target) if target else None
        if entity is None or entity.location_id != state.location_id:
            return RuleOutcome(
                ok=False,
                code="not_in_combat",
                message="You are not in combat with anyone here.",
            )
        combatant = self.combatant_for(entity, state)
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
