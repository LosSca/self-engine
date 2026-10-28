"""Deterministic utterance parsing: verb + target resolution."""
from app.models.game import WorldDefinition
from app.services.text_action_service import TextActionParser
from tests.support import fresh_state, village_world, village_world_data


def _chinese_world() -> WorldDefinition:
    """The demo world with Chinese display names, ids kept in ASCII."""
    data = village_world_data()
    for item in data["items"]:
        if item["id"] == "iron_key":
            item["name"] = "铁钥匙"
    for door in data["doors"]:
        if door["id"] == "warehouse_door":
            door["name"] = "仓库门"
    for location in data["locations"]:
        if location["id"] in ("village_square", "forest_edge", "warehouse"):
            location["name"] = {"village_square": "村庄广场", "forest_edge": "森林边缘", "warehouse": "仓库"}[location["id"]]
    return WorldDefinition.model_validate(data)


def test_ascii_id_resolves_english_spelling():
    """An author id of iron_key accepts "iron key" typed by the player."""
    world = _chinese_world()
    state = fresh_state(world)
    parsed = TextActionParser().parse("pick up the iron key", world, state)
    assert parsed is not None
    assert parsed.action == "take"
    assert parsed.target_id == "iron_key"


def test_underscore_id_resolves_when_player_types_underscore():
    world = _chinese_world()
    state = fresh_state(world)
    parsed = TextActionParser().parse("open warehouse_door", world, state)
    assert parsed is not None
    assert parsed.action == "open"
    assert parsed.target_id == "warehouse_door"


def test_chinese_display_name_still_matches():
    world = _chinese_world()
    state = fresh_state(world)
    parsed = TextActionParser().parse("打开仓库门", world, state)
    assert parsed is not None
    assert parsed.action == "open"
    assert parsed.target_id == "warehouse_door"


def test_unknown_target_is_reported_not_guessed():
    world = _chinese_world()
    state = fresh_state(world)
    parsed = TextActionParser().parse("take the moon", world, state)
    assert parsed is not None
    assert parsed.action == "take"
    assert parsed.target_id is None
    assert parsed.confidence == "missing_target"
