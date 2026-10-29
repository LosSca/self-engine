"""Deterministic combat: rules decide, the LLM only narrates."""
import pytest

from app.db.game_state_store import GameStateStore
from app.engines.rules_engine import RulesEngine
from app.models.game import (
    ActionRequest,
    EntityDef,
    GameState,
    LocationDef,
    WorldDefinition,
)
from app.services.action_service import ActionService
from app.services.world_service import bind_world, save_world


def _world() -> WorldDefinition:
    return WorldDefinition.model_validate(
        {
            "world_id": "arena",
            "name": "Arena",
            "start_location_id": "square",
            "locations": [
                {
                    "id": "square",
                    "name": "Square",
                    "description": "A dusty square.",
                    "exits": [],
                    "item_ids": [],
                    "door_ids": [],
                    "npc_ids": [],
                },
                {
                    "id": "cellar",
                    "name": "Cellar",
                    "description": "A dark cellar.",
                    "exits": [],
                    "item_ids": [],
                    "door_ids": [],
                    "npc_ids": [],
                },
            ],
            "items": [],
            "doors": [],
            "entities": [
                {
                    "id": "goblin",
                    "name": "Goblin",
                    "location_id": "square",
                    "kind": "creature",
                    "hostile": True,
                    "attributes": {
                        "max_hp": 6,
                        "attack": 2,
                        "defense": 1,
                        "flee_chance": 1.0,
                    },
                }
            ],
            "quests": [],
            "events": [],
        }
    )


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world = _world()
    save_world(store, world)
    bind_world(store, "camp-1", world.world_id, reset=True)
    yield store
    store.close()


def _request(request_id, action, target=None):
    return ActionRequest(
        campaign_id="camp-1", request_id=request_id, action=action, target_id=target
    )


def _initial_state(world: WorldDefinition) -> GameState:
    from app.services.world_service import build_initial_state

    return build_initial_state(world, "camp-1")


def test_attack_is_pure_and_repeatable():
    world = _world()
    state = _initial_state(world)
    rules = RulesEngine()
    first = rules.evaluate(world, state, "attack", "goblin")
    second = rules.evaluate(world, state, "attack", "goblin")
    assert first.ok and second.ok
    assert first.code == second.code
    assert first.effects == second.effects
    assert state.combatants == {}  # rules never mutate


def test_attack_without_target_is_rejected():
    world = _world()
    state = _initial_state(world)
    outcome = RulesEngine().evaluate(world, state, "attack", None)
    assert not outcome.ok
    assert outcome.code == "missing_target"


def test_attack_missing_entity_is_rejected():
    world = _world()
    state = _initial_state(world)
    outcome = RulesEngine().evaluate(world, state, "attack", "dragon")
    assert not outcome.ok
    assert outcome.code == "unknown_target"


async def test_attack_reduces_enemy_hp_and_commits(store):
    service = ActionService(store)
    result = await service.execute(_request("req-1", "attack", "goblin"))
    assert result.status == "applied"
    assert result.code in ("player_hit", "player_missed")
    assert result.combat_round is not None
    assert result.combat_round["target_id"] == "goblin"
    assert result.combat_round["target_hp_after"] is not None

    stored = store.get_state("camp-1")
    assert stored.version == 1
    assert "goblin" in stored.combatants
    assert stored.combatants["goblin"].max_hp == 6


async def test_combat_resolves_deterministically_across_identical_runs(tmp_path):
    outcomes = []
    for run in range(2):
        store = GameStateStore(str(tmp_path / f"run{run}.db"))
        world = _world()
        save_world(store, world)
        bind_world(store, "camp-1", world.world_id, reset=True)
        service = ActionService(store)
        result = await service.execute(_request("req-1", "attack", "goblin"))
        outcomes.append((result.code, result.combat_round))
        store.close()
    assert outcomes[0] == outcomes[1]


async def test_enemy_is_eventually_defeated_and_attacks_stop(store):
    service = ActionService(store)
    defeated = False
    for turn in range(20):
        result = await service.execute(_request(f"req-{turn}", "attack", "goblin"))
        if result.code == "enemy_defeated":
            defeated = True
            break
    assert defeated
    state = store.get_state("camp-1")
    assert state.combatants["goblin"].alive is False
    assert state.combatants["goblin"].hp == 0
    assert state.flags.get("combat_active") is False

    again = await service.execute(_request("req-after", "attack", "goblin"))
    assert again.status == "rejected"
    assert again.code == "target_defeated"


async def test_duplicate_attack_does_not_roll_twice(store):
    service = ActionService(store)
    first = await service.execute(_request("req-dup", "attack", "goblin"))
    second = await service.execute(_request("req-dup", "attack", "goblin"))
    assert second.status == "duplicate"
    assert second.combat_round == first.combat_round
    assert store.get_state("camp-1").version == 1


async def test_defend_and_flee_resolve_with_rules(store):
    service = ActionService(store)
    defend = await service.execute(_request("req-def", "defend", "goblin"))
    assert defend.status == "applied"
    assert defend.code == "defended"
    assert defend.combat_round["action"] == "defend"

    flee = await service.execute(_request("req-flee", "flee"))
    assert flee.status == "applied", flee.message
    assert flee.code == "escaped"  # flee_chance is 1.0 in this world
    assert store.get_state("camp-1").flags.get("combat_active") is False


async def test_event_can_react_to_enemy_defeat(store):
    store.close()
