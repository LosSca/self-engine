"""The text pipeline gains no privileges from `actor_type`.

`TextActionService` is the natural-language entry for the *player*: it records
`actor_type="player"` on every stored request and refuses to replay a request
that was written under another actor. An NPC never comes through here - it
submits an `ActionRequest` directly - so this file pins that the parser cannot
be used as a back door into another actor's turn.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import ActionRequest
from app.services.text_action_service import TextActionService
from app.services.world_service import bind_world, save_world
from tests.support import village_world


@pytest.fixture
def service_and_store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world = village_world()
    save_world(store, world)
    bind_world(store, "camp-1", world.world_id, reset=True)
    yield TextActionService(store), store
    store.close()


async def test_text_turn_is_recorded_as_a_player_turn(service_and_store):
    service, store = service_and_store
    await service.execute("camp-1", "环顾四周", request_id="txt-1", narrate=False)

    row = store.get_request("txt-1")
    assert row is not None
    assert row["request"]["actor_type"] == "player"
    assert row["request"]["actor_id"] == "player"


async def test_replay_of_a_text_request_keeps_the_player_actor(service_and_store):
    service, _store = service_and_store
    first = await service.execute("camp-1", "环顾四周", request_id="txt-2", narrate=False)
    replay = await service.execute("camp-1", "环顾四周", request_id="txt-2", narrate=False)

    assert replay["status"] == "duplicate"
    assert replay["result"]["actor_type"] == "player"
    assert replay["result"]["state_version"] == first["result"]["state_version"]


async def test_a_request_written_under_another_actor_is_not_replayed_as_text(
    service_and_store,
):
    """An NPC request must never be answered by the text lane's replay path."""
    service, store = service_and_store
    npc_request = ActionRequest(
        campaign_id="camp-1",
        request_id="npc-1",
        action="look",
        actor_type="npc",
        narrate=False,
    )
    store.record_request(
        campaign_id="camp-1",
        request_id="npc-1",
        request_payload=npc_request.model_dump(mode="json"),
        status="applied",
        result_payload={"request_id": "npc-1", "campaign_id": "camp-1"},
        state_version=0,
    )

    result = await service.execute("camp-1", "环顾四周", request_id="npc-1", narrate=False)
    assert result["status"] == "conflict"
    assert result["result"]["code"] == "request_id_conflict"
