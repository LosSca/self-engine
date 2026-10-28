"""World definitions are configuration: bad configuration must fail loudly."""
import copy

import pytest
from pydantic import ValidationError

from app.models.game import WorldDefinition
from tests.support import village_world, village_world_data


def test_village_fixture_is_valid():
    assert village_world().validate_references() == []


def test_duplicate_location_ids_are_rejected():
    data = village_world_data()
    data["locations"].append(copy.deepcopy(data["locations"][0]))
    errors = WorldDefinition.model_validate(data).validate_references()
    assert any("duplicate location" in error for error in errors), errors


def test_unknown_exit_target_is_rejected():
    data = village_world_data()
    data["locations"][0]["exits"][0]["target_id"] = "nowhere"
    errors = WorldDefinition.model_validate(data).validate_references()
    assert any("nowhere" in error for error in errors), errors


def test_unknown_door_key_is_rejected():
    data = village_world_data()
    data["doors"][0]["key_item_id"] = "ghost_key"
    errors = WorldDefinition.model_validate(data).validate_references()
    assert any("ghost_key" in error for error in errors), errors


def test_unknown_quest_objective_target_is_rejected():
    data = village_world_data()
    data["quests"][0]["objectives"][0]["target"] = "ghost_item"
    errors = WorldDefinition.model_validate(data).validate_references()
    assert any("ghost_item" in error for error in errors), errors


def test_event_trigger_action_is_required():
    data = village_world_data()
    data["events"][0].pop("trigger_action")
    errors = WorldDefinition.model_validate(data).validate_references()
    assert any("trigger_action" in error for error in errors), errors


def test_unknown_effect_kind_is_rejected_by_the_model():
    data = village_world_data()
    data["events"][0]["effects"][0]["kind"] = "grant_wish"
    with pytest.raises(ValidationError):
        WorldDefinition.model_validate(data)


def test_unknown_condition_kind_is_rejected_by_the_model():
    data = village_world_data()
    data["events"][0]["conditions"][0]["kind"] = "roll_dice"
    with pytest.raises(ValidationError):
        WorldDefinition.model_validate(data)


def test_extra_fields_are_forbidden():
    data = village_world_data()
    data["locations"][0]["colour"] = "red"
    with pytest.raises(ValidationError):
        WorldDefinition.model_validate(data)


def test_condition_with_unknown_quest_status_value_is_rejected():
    data = village_world_data()
    data["events"][0]["conditions"] = [
        {"kind": "quest_status", "target": "open_the_warehouse", "value": "in_progress"}
    ]
    errors = WorldDefinition.model_validate(data).validate_references()
    assert any("unknown quest status" in error for error in errors), errors
