"""fallback 叙事必须把规则已经说出口的话带出来。"""
from app.engines.rules_engine import RulesEngine
from app.models.game import EntityDef, GameState, LocationDef, WorldDefinition
from app.services.narrative_service import describe_effects, render_fallback


def _world():
    return WorldDefinition(
        world_id="w1",
        name="环形站",
        language="zh",
        start_location_id="corridor",
        locations=[LocationDef(id="corridor", name="环形走廊", npc_ids=["traveler"])],
        entities=[
            EntityDef(id="traveler", name="旅人", location_id="corridor",
                      dialogue=["这里以前是观星台。"]),
        ],
    )


def test_talking_produces_a_line_in_the_fallback_narration():
    world = _world()
    state = GameState(campaign_id="c1", world_id="w1", location_id="corridor")
    outcome = RulesEngine()._talk(world, state, "traveler", {})
    text = render_fallback(world, state, outcome, [])
    assert "观星台" in text, text
    assert "Nothing changes" not in text


def test_a_silent_person_still_gets_a_sentence():
    world = _world()
    world.entities[0].dialogue = []
    state = GameState(campaign_id="c1", world_id="w1", location_id="corridor")
    outcome = RulesEngine()._talk(world, state, "traveler", {})
    text = render_fallback(world, state, outcome, [])
    assert "旅人" in text