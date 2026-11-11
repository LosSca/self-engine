"""Conversational completion: proposals are staged, reviewed, then committed."""
import copy

import pytest

from app.models.knowledge import KnowledgeEntryCreate
from app.services import authoring_session, world_service
from tests.support import village_world


def _location_op(identifier="river_bank", name="River Bank", exits=("forest_edge",)):
    return {
        "kind": "add_location",
        "data": {
            "id": identifier,
            "name": name,
            "description": "Wet stones and reed.",
            "exits": [{"target_id": target} for target in exits],
        },
    }


def test_propose_stages_without_writing():
    world = village_world()
    before = [loc.id for loc in world.locations]
    reply, proposal = authoring_session.propose(
        {"reply": "好", "ops": [_location_op()]}, world, []
    )
    assert reply == "好"
    assert [loc.id for loc in world.locations] == before
    assert "river_bank" in [loc.id for loc in proposal["world"].locations]
    assert proposal["changes"] == [{"label": "地点", "before": 3, "after": 4}]
    assert proposal["empty"] is False


def test_propose_reports_an_invalid_operation_and_keeps_the_rest():
    world = village_world()
    ops = [
        {"kind": "add_door", "data": {"id": "ghost_door", "name": "Ghost", "location_id": "village_square", "locked": True, "key_item_id": "nope"}},
        _location_op(),
    ]
    _, proposal = authoring_session.propose({"reply": "", "ops": ops}, world, [])
    assert [op["kind"] for op in proposal["ops"]] == ["add_location"]
    assert any("ghost_door" in warning for warning in proposal["warnings"])


def test_propose_rejects_duplicate_ids_and_unknown_kinds():
    world = village_world()
    ops = [
        _location_op("village_square", "Again"),
        {"kind": "delete_everything", "data": {}},
        {"kind": "add_item", "data": {"name": "no id"}},
    ]
    _, proposal = authoring_session.propose({"reply": "", "ops": ops}, world, [])
    assert proposal["ops"] == []
    assert proposal["empty"] is True
    assert len(proposal["warnings"]) == 3


def test_propose_turns_notes_into_knowledge_and_skips_known_titles():
    world = village_world()
    existing = [KnowledgeEntryCreate(layer="global", title="Old Lore", content="x")]
    ops = [
        {"kind": "set_note", "data": {"title": "New Lore", "content": "The river is cold."}},
        {"kind": "set_note", "data": {"title": "Old Lore", "content": "Duplicate."}},
        {"kind": "add_knowledge", "data": {"layer": "global", "title": "", "content": "x"}},
    ]
    _, proposal = authoring_session.propose({"reply": "", "ops": ops}, world, existing)
    assert proposal["knowledge_titles"] == ["New Lore"]
    assert any("Old Lore" in warning for warning in proposal["warnings"])
    assert any("未通过校验" in warning for warning in proposal["warnings"])


def test_propose_handles_bad_model_output_without_raising():
    world = village_world()
    for payload in (None, "text", [], {"reply": "hi", "ops": "nope"}):
        reply, proposal = authoring_session.propose(payload, world, [])
        assert isinstance(reply, str)
        assert proposal["ops"] == []
        assert proposal["empty"] is True


@pytest.mark.asyncio
async def test_propose_edits_survives_a_broken_model():
    class Broken:
        async def complete(self, messages, max_tokens=0, temperature=0.0):
            raise RuntimeError("provider down")

    world = village_world()
    reply, proposal = await authoring_session.propose_edits(Broken(), "加一条河", world, [])
    assert reply == ""
    assert proposal["ops"] == []
    assert any("provider down" in warning for warning in proposal["warnings"])


def test_commit_writes_the_world_document(tmp_path):
    from app.db.game_state_store import GameStateStore

    world = village_world()
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world_service.save_world(store, world)
    _, proposal = authoring_session.propose({"reply": "", "ops": [_location_op()]}, world, [])

    class NoKnowledge:
        def add_knowledge(self, scenario_id, entry):
            raise AssertionError("no knowledge in this proposal")

    result = authoring_session.commit_proposal(
        proposal, world, NoKnowledge(), store, world.scenario_id
    )
    assert result["world_saved"] is True
    saved = store.get_world(world.world_id)
    assert "river_bank" in [loc.id for loc in saved.locations]
    store.close()


def test_commit_refuses_a_dangling_reference(tmp_path):
    from app.db.game_state_store import GameStateStore

    world = village_world()
    store = GameStateStore(str(tmp_path / "game_state.db"))
    world_service.save_world(store, world)
    draft = copy.deepcopy(world)
    broken = draft.model_copy(deep=True)
    broken.locations[0].exits = [
        type(broken.locations[0].exits[0])(target_id="nowhere")
    ]
    proposal = {"world": broken, "knowledge": []}

    class NoKnowledge:
        def add_knowledge(self, scenario_id, entry):
            return None

    result = authoring_session.commit_proposal(
        proposal, world, NoKnowledge(), store, world.scenario_id
    )
    assert result["world_saved"] is False
    assert any("nowhere" in warning for warning in result["warnings"])
    assert store.get_world(world.world_id).locations[0].exits[0].target_id != "nowhere"
    store.close()
