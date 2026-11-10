"""Starter worlds: every campaign can enter the rules lane.

A campaign whose scenario has no authored world used to fall back to the legacy
LLM session, which keeps its own state -- so the two systems disagreed about the
same player. These tests pin the replacement behaviour: bind what exists, else
generate a validated world, deterministically, without overwriting anything.
"""
import pytest

from app.db.game_state_store import GameStateStore
from app.db.scenario_store import ScenarioStore
from app.models.game import WorldDefinition
from app.services import starter_worlds, world_service
from app.services.text_action_service import TextActionService


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenario.db"))
    scenario_store = ScenarioStore(str(tmp_path / "scenario.db"))
    game_store = GameStateStore(str(tmp_path / "game.db"))
    yield game_store, scenario_store
    game_store.close()
    scenario_store.close()


def _campaign(scenario_store, **kwargs):
    scenario = scenario_store.create_scenario(
        title=kwargs.pop("title", "Mist Inn"),
        language=kwargs.pop("language", "en"),
        **kwargs,
    )
    return scenario_store.create_campaign(scenario.id, "Tester"), scenario


def test_generated_worlds_are_valid_for_every_shape():
    for language in ("中文", "en"):
        for world_type in ("", "fantasy", "orbiting-nonsense"):
            document = starter_worlds.build_world_document(
                scenario_id="sc-1",
                world_id=starter_worlds.starter_world_id("camp-1"),
                title="Mist Inn",
                summary="A tavern inside the fog.",
                language=language,
                world_type=world_type,
            )
            world = WorldDefinition(**document)
            assert world_service.validate_world(world) == []
            assert world.start_location_id in {loc.id for loc in world.locations}
            assert world.doors and world.quests


def test_generation_is_deterministic():
    first = starter_worlds.build_world_document("sc-1", "w-1", "Mist Inn", "Fog.", "en")
    second = starter_worlds.build_world_document("sc-1", "w-1", "Mist Inn", "Fog.", "en")
    assert first == second


def test_campaign_without_world_gets_a_bound_starter_world(stores):
    game_store, scenario_store = stores
    campaign, _scenario = _campaign(scenario_store)
    service = TextActionService(game_store, scenario_store=scenario_store)

    world = service.bindable_world(campaign.id)

    assert world is not None
    assert world.scenario_id == _scenario.id
    state = game_store.get_state(campaign.id)
    assert state is not None
    assert state.world_id == world.world_id
    assert state.location_id == world.start_location_id
    # The generated quest is part of the bound state, so progress can advance.
    assert "q_first_steps" in state.quests


def test_binding_is_idempotent(stores):
    game_store, scenario_store = stores
    campaign, _scenario = _campaign(scenario_store)
    service = TextActionService(game_store, scenario_store=scenario_store)

    first = service.bindable_world(campaign.id)
    second = service.bindable_world(campaign.id)

    assert first is not None
    assert second is None  # already bound -> caller keeps the bound state
    assert [w["world_id"] for w in game_store.list_worlds()] == [first.world_id]


def test_authored_world_for_the_scenario_wins_over_generation(stores):
    game_store, scenario_store = stores
    campaign, scenario = _campaign(scenario_store)
    authored = WorldDefinition(
        world_id="authored_world",
        scenario_id=scenario.id,
        name="Authored",
        start_location_id="here",
        locations=[{"id": "here", "name": "Here", "description": "A room."}],
    )
    world_service.save_world(game_store, authored)
    service = TextActionService(game_store, scenario_store=scenario_store)

    bound = service.bindable_world(campaign.id)

    assert bound is not None
    assert bound.world_id == "authored_world"
    assert len(game_store.list_worlds()) == 1


def test_existing_world_id_is_never_overwritten(stores):
    game_store, scenario_store = stores
    campaign, _scenario = _campaign(scenario_store)
    base_id = starter_worlds.starter_world_id(campaign.id)
    occupied = WorldDefinition(
        world_id=base_id,
        name="Hand edited",
        start_location_id="here",
        locations=[{"id": "here", "name": "Here", "description": "A room."}],
    )
    world_service.save_world(game_store, occupied)
    service = TextActionService(game_store, scenario_store=scenario_store)

    world = service.bindable_world(campaign.id)

    assert world is not None
    assert world.world_id != base_id
    assert world.world_id == starter_worlds.alternate_world_id(base_id, 1)
    assert game_store.get_world(base_id).name == "Hand edited"


def test_state_whose_world_vanished_still_gets_a_world(stores):
    game_store, scenario_store = stores
    campaign, _scenario = _campaign(scenario_store)
    service = TextActionService(game_store, scenario_store=scenario_store)
    bound = service.bindable_world(campaign.id)
    assert bound is not None
    state = game_store.get_state(campaign.id)
    assert game_store.delete_world(bound.world_id) is True

    replacement = service._world_for_state(state, campaign.id)

    # The freed id is reused, so the campaign keeps pointing at a live world.
    assert replacement.world_id == bound.world_id
    assert game_store.get_world(replacement.world_id) is not None
    assert game_store.get_state(campaign.id) is not None


def test_starter_world_accepts_a_full_turn(stores):
    """The generated world must be playable, not merely valid."""
    import asyncio

    game_store, scenario_store = stores
    campaign, _scenario = _campaign(scenario_store)
    service = TextActionService(game_store, scenario_store=scenario_store)
    service.bindable_world(campaign.id)

    opened_without_key = asyncio.run(
        service.execute(campaign.id, "open the fallen gate", request_id="r-open")
    )
    assert opened_without_key["status"] == "rejected"
    assert opened_without_key["result"]["code"] == "door_locked"

    taken = asyncio.run(
        service.execute(campaign.id, "take the traveler's key", request_id="r-take")
    )
    assert taken["status"] == "applied"

    opened = asyncio.run(
        service.execute(campaign.id, "open the fallen gate", request_id="r-open-2")
    )
    assert opened["status"] == "applied"
    assert opened["result"]["state"]["doors"]["inner_door"] is True


def test_chinese_scenario_gets_a_chinese_starter_world(stores):
    """The stores keep "中文" while the UI sends "zh"; both must localise."""
    game_store, scenario_store = stores
    campaign, _scenario = _campaign(scenario_store, language="中文")
    service = TextActionService(game_store, scenario_store=scenario_store)

    world = service.bindable_world(campaign.id)

    names = [location.name for location in world.locations]
    assert names == ["落脚处", "里侧", "旁路"]
    assert [door.name for door in world.doors] == ["倒落的门"]
