"""Interaction planning: room plans, action previews and requirement chains."""
from __future__ import annotations

import json

import pytest

from app.models.game import Effect
from app.services import interaction_planner as planner
from app.services.world_service import build_initial_state
from tests.support import village_world


def _state(world, location_id="village_square"):
    state = build_initial_state(world, "camp-1")
    state.location_id = location_id
    return state


def test_plan_room_asks_the_rules_engine_for_every_candidate():
    world = village_world()
    plan = planner.plan_room(world, _state(world))
    allowed = {(row["action"], row["target"]) for row in plan["allowed"]}
    blocked = {(row["action"], row["target"]): row["code"] for row in plan["blocked"]}
    assert ("take", "iron_key") in allowed
    assert ("move", "forest_edge") in allowed
    # The warehouse is behind a locked door, so it is not walkable yet.
    assert blocked[("move", "warehouse")] == "exit_blocked"
    assert blocked[("open", "warehouse_door")] == "door_locked"
    assert plan["version"] == 0


def test_plan_room_does_not_mutate_the_state():
    world = village_world()
    state = _state(world)
    before = state.model_dump()
    planner.plan_room(world, state)
    assert state.model_dump() == before


def test_preview_reports_what_each_step_would_change():
    world = village_world()
    state = _state(world)
    preview = planner.run_preview(
        world, state, [{"action": "take", "target": "iron_key"}]
    )
    row = preview["steps"][0]
    assert row["ok"] is True and row["advanced"] is True
    fields = {(item["field"], item["target"]) for item in row["changes"]}
    assert ("inventory", "iron_key") in fields
    assert ("item_locations", "iron_key") in fields
    assert preview["simulated"] is True
    assert preview["start_version"] == state.version


def test_preview_never_touches_the_real_state():
    world = village_world()
    state = _state(world)
    before = state.model_dump()
    planner.run_preview(
        world,
        state,
        [{"action": "take", "target": "iron_key"}, {"action": "open", "target": "warehouse_door"}],
    )
    assert state.model_dump() == before
    assert state.version == 0 and state.turn == 0


def test_preview_settles_events_the_way_a_turn_would():
    world = village_world()
    state = _state(world)
    world = world.model_copy(deep=True)
    # events[1] is key_pickup_noted: triggers on take with no conditions.
    world.events.append(
        world.events[1].model_copy(
            update={
                "id": "on_take",
                "trigger_target": None,
                "effects": [Effect(kind="set_flag", target="take_seen", value=True)],
            }
        )
    )
    preview = planner.run_preview(
        world, state, [{"action": "take", "target": "iron_key"}]
    )
    fired = {row["event_id"]: row["status"] for row in preview["steps"][0]["events"]}
    assert fired["on_take"] == "fired"
    assert fired["key_pickup_noted"] == "fired"
    row = preview["steps"][0]
    flags = {(item["field"], item["target"]): item["after"] for item in row["changes"]}
    assert flags[("flags", "take_seen")] is True


def test_preview_stops_at_the_first_rejected_step():
    world = village_world()
    state = _state(world)
    preview = planner.run_preview(
        world,
        state,
        [
            {"action": "open", "target": "warehouse_door"},
            {"action": "take", "target": "iron_key"},
        ],
    )
    assert len(preview["steps"]) == 1
    assert preview["steps"][0]["code"] == "door_locked"
    assert preview["stopped_at"] == 1


def test_preview_rejects_empty_and_oversized_chains():
    world = village_world()
    state = _state(world)
    with pytest.raises(planner.PlanError):
        planner.run_preview(world, state, [])
    too_many = [{"action": "look", "target": ""}] * (planner.MAX_PREVIEW_STEPS + 1)
    with pytest.raises(planner.PlanError):
        planner.run_preview(world, state, too_many)
    with pytest.raises(planner.PlanError):
        planner.run_preview(world, state, [{"target": "x"}])


def test_explain_names_the_key_and_how_to_get_it():
    world = village_world()
    state = _state(world)
    answer = planner.explain_requirement(world, state, "open", "warehouse_door")
    assert answer["ok"] is False and answer["code"] == "door_locked"
    assert answer["has_plan"] is True
    plan = answer["requirements"][0]
    assert plan["target"] == "iron_key"
    assert plan["distance"] == 0
    assert plan["steps"] == ["take iron_key"]


def test_explain_stops_planning_once_the_action_is_allowed():
    world = village_world()
    state = _state(world)
    state.inventory["iron_key"] = 1
    state.item_locations.pop("iron_key", None)
    answer = planner.explain_requirement(world, state, "open", "warehouse_door")
    assert answer["ok"] is True
    assert answer["requirements"] == []


def test_explain_reports_the_distance_to_an_item_in_another_room():
    world = village_world()
    data = json.loads(json.dumps(world.model_dump()))
    data["items"].append(
        {
            "id": "copper_key",
            "name": "Copper Key",
            "pickup_requires": [{"kind": "at_location", "target": "forest_edge"}],
        }
    )
    world = type(world).model_validate(data)
    state = _state(world)
    state.item_locations["copper_key"] = "forest_edge"
    answer = planner.explain_requirement(world, state, "take", "copper_key")
    # The rules answer first (the item is not in this room); the planner still
    # explains the authored pickup guard as a reachable precondition.
    assert answer["ok"] is False
    plan = answer["requirements"][0]
    assert plan["kind"] == "reach_location"
    assert plan["target"] == "forest_edge"
    assert plan["distance"] == 1
    assert plan["steps"] == ["move forest_edge"]
