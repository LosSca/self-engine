"""World definitions and campaign state bootstrap.

A world is configuration data: locations, exits, items, doors, quests and
events. Nothing about the demo village lives in code — it is loaded, validated
and then persisted as a row so the same engine can host any world.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db.game_state_store import GameStateStore
from app.engines.quest_engine import initial_quest_progress
from app.models.game import GameState, WorldDefinition


class WorldValidationError(ValueError):
    """Raised when a world definition is itself inconsistent."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors) if errors else "invalid world")


def validate_world(world: WorldDefinition) -> List[str]:
    return world.validate_references()


def save_world(store: GameStateStore, world: WorldDefinition) -> Dict[str, Any]:
    errors = validate_world(world)
    if errors:
        raise WorldValidationError(errors)
    store.save_world(world)
    return {
        "world_id": world.world_id,
        "name": world.name,
        "scenario_id": world.scenario_id,
        "locations": len(world.locations),
        "items": len(world.items),
        "doors": len(world.doors),
        "quests": len(world.quests),
        "events": len(world.events),
    }


def get_world(store: GameStateStore, world_id: str) -> WorldDefinition:
    world = store.get_world(world_id)
    if world is None:
        raise LookupError(f"unknown world '{world_id}'")
    return world


def build_initial_state(
    world: WorldDefinition, campaign_id: str, turn: int = 0
) -> GameState:
    """Derive the fresh authoritative state of a campaign from its world."""
    item_locations: Dict[str, str] = {}
    for location in world.locations:
        for item_id in location.item_ids:
            item_locations[item_id] = location.id
    return GameState(
        campaign_id=campaign_id,
        world_id=world.world_id,
        version=0,
        turn=turn,
        location_id=world.start_location_id,
        inventory={},
        flags={},
        doors={door.id: door.opened for door in world.doors},
        item_locations=item_locations,
        quests=initial_quest_progress(world),
        fired_events=[],
        skills=dict(world.starting_skills),
        updated_at=datetime.utcnow().isoformat(),
    )


def bind_world(
    store: GameStateStore, campaign_id: str, world_id: str, reset: bool = False
) -> GameState:
    """Attach a world to a campaign, keeping existing progress by default."""
    world = get_world(store, world_id)
    existing = store.get_state(campaign_id)
    if existing is not None and not reset:
        if existing.world_id != world_id:
            raise ValueError(
                f"campaign '{campaign_id}' is bound to world '{existing.world_id}'; "
                "pass reset=true to rebind"
            )
        return existing
    state = build_initial_state(world, campaign_id)
    store.bind_campaign(state)
    return state


def get_state(store: GameStateStore, campaign_id: str) -> Optional[GameState]:
    return store.get_state(campaign_id)
