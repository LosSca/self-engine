"""World-authored actions: declarative conditions and effects, no code.

A world can describe its own verb (say "pray") as data - preconditions plus
effects - and the same rule/effect engine decides it. These tests pin the
boundaries: unknown actions fail safely, unmet preconditions are refused with a
readable reason, effects land in the authoritative state, and a configuration
that shadows a built-in verb is rejected at authoring time.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.models.game import (
    ActionRequest,
    Condition,
    CustomActionDef,
    Effect,
    WorldDefinition,
)
from app.services.action_service import ActionService
from app.services.text_action_service import TextActionParser, TextActionService
from app.services.world_service import WorldValidationError
from tests.support import fresh_state, village_world


def _pray_action(**overrides) -> CustomActionDef:
    data = {
        "id": "pray",
        "label": "祈祷",
        "description": "You press your palms together and ask the shrine for a blessing.",
        "verbs": ["祈祷", "pray"],
        "target": "none",
        "requires": [Condition(kind="has_item", target="iron_key")],
        "effects": [
            Effect(kind="set_flag", target="prayed", value=True),
            Effect(kind="give_item", target="blessing_token"),
        ],
    }
    data.update(overrides)
    return CustomActionDef(**data)


def _world_with(action: CustomActionDef) -> WorldDefinition:
    world = village_world()
    world.custom_actions = [action]
    return world


def _custom_request(request_id: str, action_id: str = "pray", target=None) -> ActionRequest:
    return ActionRequest(
        campaign_id="camp-1",
        request_id=request_id,
        action="custom",
        target_id=target,
        params={"action_id": action_id},
        narrate=False,
    )


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


def _bind(store, action: CustomActionDef):
    world = _world_with(action)
    assert world.validate_references() == []
    from app.services.world_service import bind_world, save_world

    save_world(store, world)
    state = bind_world(store, "camp-1", world.world_id, reset=True)
    return world, state


# ------------------------------------------------------------- authoring time
def test_a_world_can_declare_its_own_action():
    world = _world_with(_pray_action())
    assert world.validate_references() == []
    assert world.custom_actions[0].verbs == ["祈祷", "pray"]


def test_validation_rejects_a_verb_that_shadows_a_built_in_action():
    world = _world_with(_pray_action(verbs=["open"]))
    errors = world.validate_references()
    assert any("already mapped to a built-in action" in e for e in errors), errors


def test_validation_rejects_a_name_that_collides_with_a_built_in_action():
    world = _world_with(_pray_action(id="open"))
    errors = world.validate_references()
    assert any("collides with a built-in action name" in e for e in errors), errors


def test_validation_rejects_an_action_without_verbs():
    world = _world_with(_pray_action(verbs=[]))
    errors = world.validate_references()
    assert any("declares no verbs" in e for e in errors), errors


def test_validation_rejects_a_condition_on_an_unknown_item():
    world = _world_with(
        _pray_action(requires=[Condition(kind="has_item", target="ghost_item")])
    )
    errors = world.validate_references()
    assert any("unknown item 'ghost_item'" in e for e in errors), errors


def test_validation_rejects_an_effect_on_an_unknown_quest():
    world = _world_with(
        _pray_action(effects=[Effect(kind="quest_complete", target="ghost_quest")])
    )
    errors = world.validate_references()
    assert any("unknown quest 'ghost_quest'" in e for e in errors), errors


def test_validation_rejects_two_actions_sharing_a_verb():
    world = _world_with(_pray_action())
    world.custom_actions.append(
        _pray_action(id="chant", label="吟唱", verbs=["pray", "chant"])
    )
    errors = world.validate_references()
    assert any("is already used by custom action 'pray'" in e for e in errors), errors


def test_the_world_editor_refuses_an_invalid_action(store):
    from app.services import world_editor

    world = village_world()
    from app.services.world_service import save_world

    save_world(store, world)
    with pytest.raises(WorldValidationError) as excinfo:
        world_editor.upsert_custom_action(world, _pray_action(verbs=["take"]), "pray")
    assert "already mapped to a built-in action" in str(excinfo.value)
    # The stored document is untouched by the rejected edit.
    assert store.get_world(world.world_id).custom_actions == []


# ---------------------------------------------------------------- rule engine
async def test_custom_action_is_refused_while_the_condition_is_unmet(store):
    _bind(store, _pray_action())
    service = ActionService(store)

    result = await service.execute(_custom_request("req-pray-1"))

    assert result.status == "rejected"
    assert result.code == "condition_failed"
    assert "you need 'iron_key'" in result.message
    assert result.state_version == 0
    assert store.get_state("camp-1").flags.get("prayed") is None


async def test_custom_action_applies_its_effects_once_allowed(store):
    _bind(store, _pray_action())
    service = ActionService(store)

    taken = await service.execute(
        ActionRequest(
            campaign_id="camp-1",
            request_id="req-take",
            action="take",
            target_id="iron_key",
            narrate=False,
        )
    )
    assert taken.status == "applied"

    prayed = await service.execute(_custom_request("req-pray-2"))

    assert prayed.status == "applied"
    assert prayed.code == "custom_action"
    assert prayed.state_version == 2
    state = store.get_state("camp-1")
    assert state.flags["prayed"] is True
    assert state.inventory == {"iron_key": 1, "blessing_token": 1}


async def test_unknown_custom_action_fails_safely(store):
    _bind(store, _pray_action())
    service = ActionService(store)

    result = await service.execute(_custom_request("req-unknown", action_id="dance"))

    assert result.status == "rejected"
    assert result.code == "unknown_custom_action"
    assert "dance" in result.message
    assert result.state_version == 0


async def test_custom_action_without_action_id_fails_safely(store):
    _bind(store, _pray_action())
    service = ActionService(store)

    result = await service.execute(
        ActionRequest(
            campaign_id="camp-1",
            request_id="req-noid",
            action="custom",
            narrate=False,
        )
    )

    assert result.status == "rejected"
    assert result.code == "custom_action_missing"


async def test_a_required_target_is_enforced(store):
    _bind(
        store,
        _pray_action(
            id="bless",
            label="祝福",
            verbs=["祝福", "bless"],
            target="required",
            requires=[],
            effects=[Effect(kind="set_flag", target="blessed", value=True)],
        ),
    )
    service = ActionService(store)

    result = await service.execute(_custom_request("req-bless", action_id="bless"))

    assert result.status == "rejected"
    assert result.code == "custom_target_required"
    assert "祝福" in result.message


async def test_an_unknown_target_is_rejected(store):
    _bind(
        store,
        _pray_action(
            id="bless",
            label="Bless",
            verbs=["bless"],
            target="required",
            requires=[],
            effects=[Effect(kind="set_flag", target="blessed", value=True)],
        ),
    )
    service = ActionService(store)

    result = await service.execute(
        _custom_request("req-bless-2", action_id="bless", target="nothing_here")
    )

    assert result.status == "rejected"
    assert result.code == "unknown_target"


async def test_repeating_the_same_request_does_not_grant_the_item_twice(store):
    _bind(store, _pray_action())
    service = ActionService(store)
    await service.execute(
        ActionRequest(
            campaign_id="camp-1",
            request_id="req-take",
            action="take",
            target_id="iron_key",
            narrate=False,
        )
    )
    first = await service.execute(_custom_request("req-pray-3"))
    again = await service.execute(_custom_request("req-pray-3"))

    assert first.status == "applied"
    assert again.status == "duplicate"
    assert store.get_state("camp-1").inventory["blessing_token"] == 1


# -------------------------------------------------------------- text parsing
def test_parser_maps_a_world_verb_onto_the_custom_action():
    world = _world_with(_pray_action())
    state = fresh_state(world)

    parsed = TextActionParser().parse("祈祷", world, state)

    assert parsed is not None
    assert parsed.action == "custom"
    assert parsed.params == {"action_id": "pray"}
    assert parsed.confidence == "verb_only"


def test_parser_still_prefers_built_in_verbs():
    world = _world_with(_pray_action())
    state = fresh_state(world)

    parsed = TextActionParser().parse("open the warehouse door", world, state)

    assert parsed.action == "open"
    assert parsed.target_id == "warehouse_door"


def test_parser_ignores_unknown_verbs():
    world = _world_with(_pray_action())
    state = fresh_state(world)

    assert TextActionParser().parse("dance wildly", world, state) is None


def test_parser_resolves_a_target_for_a_custom_action():
    world = _world_with(
        _pray_action(
            id="bless",
            label="祝福",
            verbs=["祝福", "bless"],
            target="optional",
            requires=[],
            effects=[Effect(kind="set_flag", target="blessed", value=True)],
        )
    )
    state = fresh_state(world)

    parsed = TextActionParser().parse("bless the iron key", world, state)

    assert parsed.action == "custom"
    assert parsed.target_id == "iron_key"
    assert parsed.params == {"action_id": "bless"}


async def test_text_service_runs_a_world_action_end_to_end(store):
    _bind(store, _pray_action())
    service = TextActionService(store, action_service=ActionService(store))

    taken = await service.execute("camp-1", "pick up the iron key", request_id="t1")
    assert taken["status"] == "applied"

    prayed = await service.execute("camp-1", "pray", request_id="t2")

    assert prayed["status"] == "applied"
    assert prayed["parsed"]["action"] == "custom"
    assert prayed["parsed"]["params"] == {"action_id": "pray"}
    assert store.get_state("camp-1").flags["prayed"] is True
