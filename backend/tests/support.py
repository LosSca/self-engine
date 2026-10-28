"""Shared test helpers: fixture loading only, no pytest magic."""
from __future__ import annotations

import json
from pathlib import Path

from app.models.game import GameState, WorldDefinition
from app.services.world_service import bind_world, build_initial_state, save_world

FIXTURES = Path(__file__).parent / "fixtures"


def village_world_data() -> dict:
    """Raw fixture dict, so tests can mutate it into invalid configurations."""
    return json.loads((FIXTURES / "village_world.json").read_text(encoding="utf-8"))


def village_world() -> WorldDefinition:
    return WorldDefinition.model_validate(village_world_data())


def bound_world(store, campaign_id: str = "camp-1") -> tuple:
    """Save the demo world and bind a fresh campaign to it."""
    world = village_world()
    save_world(store, world)
    state = bind_world(store, campaign_id, world.world_id, reset=True)
    return world, state


def fresh_state(world: WorldDefinition, campaign_id: str = "camp-1") -> GameState:
    return build_initial_state(world, campaign_id)
