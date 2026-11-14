"""The action pipeline must not care *who* acts, only *what* is allowed.

`actor_type` is the one field Hybrid World Simulation adds to `ActionRequest`
(see docs/hybrid-world-simulation.md). These tests pin the two invariants that
make it safe: the rules decide identically for every actor, and the actor stays
recoverable from the committed record, so an NPC's turn is traceable without a
second execution path.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import ActionRequest
from app.services.action_service import ActionService
from tests.support import bound_world


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def _request(request_id, action, actor_type="player", target=None):
    return ActionRequest(
        campaign_id="camp-1",
        request_id=request_id,
        action=action,
        actor_type=actor_type,
        target_id=target,
        narrate=False,
    )


def test_actor_type_defaults_to_player_for_existing_callers():
    assert ActionRequest(
        campaign_id="c", request_id="r", action="look"
    ).actor_type == "player"


def test_unknown_actor_type_is_rejected_by_the_model():
    with pytest.raises(ValueError):
        ActionRequest(campaign_id="c", request_id="r", action="look", actor_type="god")


async def test_rules_decide_identically_for_the_player_and_an_npc(store):
    bound_world(store)

    player_service = ActionService(store)
    player = await player_service.execute(
        _request("req-player", "take", target="iron_key")
    )
    assert player.ok and player.code == "taken"

    npc_service = ActionService(store)
    npc = await npc_service.execute(
        _request("req-npc", "look", actor_type="npc")
    )
    assert npc.ok is True
    assert npc.actor_type == "npc"


async def test_an_npc_take_is_committed_through_the_same_writer(store):
    bound_world(store)
    service = ActionService(store)

    taken = await service.execute(
        _request("req-npc-take", "take", actor_type="npc", target="iron_key")
    )
    assert taken.status == "applied"
    assert taken.ok and taken.code == "taken"
    # The NPC changed authoritative state, and it did so through the one store
    # every other actor uses - not by writing GameState directly.
    assert store.get_state("camp-1").inventory == {"iron_key": 1}
    assert taken.state_version == 1


async def test_replay_of_a_committed_request_keeps_the_actor_type(store):
    bound_world(store)
    service = ActionService(store)

    request = _request("req-npc-replay", "look", actor_type="npc")
    first = await service.execute(request)
    replay = await service.execute(request)

    assert replay.status == "duplicate"
    assert replay.actor_type == "npc"
    assert first.state_version == replay.state_version


async def test_actor_type_does_not_bypass_rule_validation(store):
    """An NPC gets no privileges: the same impossible action is rejected alike."""
    bound_world(store)
    service = ActionService(store)

    as_player = await service.execute(_request("req-p", "open", target="warehouse_door"))
    as_npc = await service.execute(
        _request("req-n", "open", actor_type="npc", target="warehouse_door")
    )

    assert as_player.status == as_npc.status == "rejected"
    assert as_player.code == as_npc.code == "door_locked"
    assert store.get_state("camp-1").version == 0
