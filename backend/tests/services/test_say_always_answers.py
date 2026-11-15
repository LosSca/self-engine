"""回归：SAY 说出口的话必须有回应，而不是被当成解析失败。"""
import pytest

from app.engines.rules_engine import RulesEngine
from app.models.game import EntityDef, GameState, LocationDef, WorldDefinition
from app.services.text_action_service import TextActionParser


def _world(language="zh"):
    return WorldDefinition(
        world_id="w1",
        name="环形站",
        start_location_id="corridor",
        language=language,
        locations=[
            LocationDef(id="corridor", name="环形走廊", description="一圈又一圈的走廊。",
                        npc_ids=["traveler"], exits=[]),
        ],
        entities=[
            EntityDef(id="traveler", name="旅人", kind="npc", location_id="corridor",
                      description="一个裹着旧披风的人。", dialogue=["这里以前是观星台。"]),
        ],
        items=[],
    )


def _state():
    return GameState(campaign_id="c1", world_id="w1", location_id="corridor")


def test_say_with_a_person_here_resolves_to_talking():
    world, state = _world(), _state()
    parsed = TextActionParser().parse("[SAY] 向旅人开口，问这里发生了什么", world, state)
    assert parsed is not None, "说话不该被解析成失败"
    assert parsed.action == "talk"
    assert parsed.target_id == "traveler"


def test_say_without_a_name_still_speaks_to_whoever_is_here():
    world, state = _world(), _state()
    parsed = TextActionParser().parse("[SAY] 这里发生了什么？", world, state)
    assert parsed is not None
    assert parsed.action == "talk"


def test_say_prose_does_not_become_an_invented_entity():
    world, state = _world(), _state()
    parsed = TextActionParser().parse("[SAY] 向旅人开口，问这里发生了什么", world, state)
    assert parsed.target_id in {entity.id for entity in world.entities}


def test_talking_gives_the_entity_a_line_to_answer_with():
    world, state = _world(), _state()
    outcome = RulesEngine()._talk(world, state, "traveler", {})
    assert outcome.ok
    assert "观星台" in outcome.message


def test_say_with_nobody_present_says_so():
    world, state = _world(), _state()
    world.entities = []
    world.locations[0].npc_ids = []
    parsed = TextActionParser().parse("[SAY] 有人吗？", world, state)
    assert parsed is not None
    assert parsed.confidence == "missing_target"


def _two_people(world):
    from app.models.game import EntityDef

    world.entities.append(
        EntityDef(id="warden", name="主管", location_id="corridor", dialogue=["别出声。"])
    )
    world.locations[0].npc_ids = ["traveler", "warden"]
    return world


def test_two_people_here_and_no_name_leaves_the_choice_open():
    world, state = _two_people(_world()), _state()
    parsed = TextActionParser().parse("[SAY] 这里发生了什么？", world, state)
    assert parsed is not None
    assert parsed.target_id is None, "两个人都在场时不该随便挑一个"
    assert parsed.confidence == "spoken"


def test_naming_one_of_two_people_picks_that_person():
    world, state = _two_people(_world()), _state()
    parsed = TextActionParser().parse("[SAY] 问主管这里发生了什么", world, state)
    assert parsed is not None
    assert parsed.target_id == "warden"


def test_a_place_named_before_the_person_is_not_mistaken_for_the_listener():
    world, state = _two_people(_world()), _state()
    parsed = TextActionParser().parse("[SAY] 在环形走廊里问主管这里发生了什么", world, state)
    assert parsed is not None
    assert parsed.target_id == "warden"


def test_speech_alone_in_a_room_still_produces_a_line():
    world, state = _world(), _state()
    outcome = RulesEngine()._talk(world, state, None, {})
    assert outcome.ok
    assert outcome.code == "talked", "屋里只有一个人时，说话应当得到回应"
    assert "观星台" in outcome.message


def test_talking_in_an_empty_room_explains_there_is_nobody():
    world, state = _world(), _state()
    world.entities = []
    world.locations[0].npc_ids = []
    outcome = RulesEngine()._talk(world, state, None, {})
    assert not outcome.ok
    assert outcome.code == "nobody_here"