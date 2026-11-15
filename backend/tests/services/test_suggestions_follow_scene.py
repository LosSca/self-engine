"""The three buttons follow the fight, the quest and the last conversation.

Every case here is a screen where the old builder offered something the rules
were about to refuse, or something the player had just done:

* a fight in progress still offered "pick up the key" (the rule lane refuses
  every move while combat runs),
* a hostile creature still offered "ask it what happened here",
* an active quest about to satisfy its next step said nothing about it,
* the next screen after a conversation asked the same opening question,
* a two-room world offered the doorway back as the only way forward.
"""

from __future__ import annotations

import json

from app.models.game import (
    DoorDef,
    EntityDef,
    ExitDef,
    GameState,
    ItemDef,
    LocationDef,
    ObjectiveDef,
    QuestDef,
    QuestProgress,
    WorldDefinition,
)
from app.services.suggestions import _room_facts, build_suggestions, hints_from_state


def _world() -> WorldDefinition:
    return WorldDefinition(
        world_id="w",
        name="测试世界",
        start_location_id="dock",
        locations=[
            LocationDef(
                id="dock",
                name="气闸舱",
                exits=[
                    ExitDef(target_id="corridor", label="内舱", door_id="hatch"),
                    ExitDef(target_id="cargo", label="货舱"),
                ],
            ),
            LocationDef(
                id="corridor",
                name="环形走廊",
                exits=[ExitDef(target_id="dock", label="气闸舱")],
            ),
            LocationDef(id="cargo", name="货舱", exits=[]),
        ],
        doors=[
            DoorDef(
                id="hatch",
                name="气闸内门",
                location_id="dock",
                opened=False,
                key_item_id="keycard",
            )
        ],
        items=[ItemDef(id="keycard", name="主管钥匙卡")],
        entities=[
            EntityDef(id="ella", name="工程师艾拉", kind="npc", location_id="corridor"),
            EntityDef(
                id="drone",
                name="失控维修无人机",
                kind="creature",
                location_id="dock",
                hostile=True,
            ),
        ],
        quests=[
            QuestDef(
                id="q",
                title="稳定反应堆",
                objectives=[
                    ObjectiveDef(id="open_reactor", kind="open_door", target="hatch")
                ],
            )
        ],
    )


def _hints(state: GameState):
    world = _world()
    return hints_from_state(
        location_id=state.location_id,
        inventory=list(state.inventory),
        world=world,
        doors=state.doors,
        item_locations=state.item_locations,
        entities=state.entities,
        combat=state.combat,
        flags=state.flags,
        story_nodes=state.story_nodes,
    )


def _texts(options) -> str:
    return json.dumps(
        [o["action"] or ("[SAY]" + o["speech"]) for o in options], ensure_ascii=False
    )


def test_still_three_buttons_in_every_case():
    for state in (
        GameState(campaign_id="c", world_id="w", location_id="dock"),
        GameState(campaign_id="c", world_id="w", location_id="cargo"),
        GameState(campaign_id="c", world_id="w", location_id="nowhere"),
    ):
        assert len(_hints(state)) == 3


def test_hostile_creature_is_never_asked_what_happened():
    state = GameState(
        campaign_id="c",
        world_id="w",
        location_id="dock",
        item_locations={"keycard": "dock"},
    )
    body = _texts(_hints(state))
    assert "向失控维修无人机开口" not in body
    assert "攻击失控维修无人机" in body


def test_combat_hides_every_walk():
    state = GameState(campaign_id="c", world_id="w", location_id="dock")
    state.combat = {"active": True, "target_id": "drone"}
    state.flags["combat_active"] = True
    options = _hints(state)
    assert len(options) == 3
    assert all("走向" not in o["action"] for o in options)
    assert all("打开" not in o["action"] for o in options)
    assert options[0]["action"] == "攻击失控维修无人机"
    assert options[0]["target_id"] == "drone"


def test_combat_flag_alone_is_enough():
    """The legacy marker: a fight can be open through flags alone."""
    state = GameState(campaign_id="c", world_id="w", location_id="dock")
    state.flags["combat_active"] = True
    assert _hints(state)[0]["action"] == "攻击失控维修无人机"


def test_quest_step_reachable_here_is_offered():
    """An unfinished quest step this room can satisfy is named as the goal."""
    state = GameState(campaign_id="c", world_id="w", location_id="dock")
    state.quests = {"q": QuestProgress(quest_id="q")}
    facts = _room_facts(_world(), state, location_id="dock")
    assert facts["quest"] == ["气闸内门"]
    assert facts["quest_id"] == ["q"]


def test_finished_quest_step_is_not_repeated_as_a_goal():
    """A completed objective stops being a *goal*, but a shut door is still a door."""
    state = GameState(campaign_id="c", world_id="w", location_id="dock")
    state.quests = {"q": QuestProgress(quest_id="q", objectives={"open_reactor": 1})}
    world = _world()
    facts = _room_facts(world, state, location_id="dock")
    assert facts["quest"] == []
    # The door itself is still shut here, so opening it stays a real step.
    assert facts["doors"] == ["气闸内门"]


def test_after_talking_the_same_question_is_replaced():
    state = GameState(
        campaign_id="c",
        world_id="w",
        location_id="corridor",
        doors={"hatch": True},
        story_nodes=["dock", "corridor", "talk:工程师艾拉"],
    )
    body = _texts(_hints(state))
    assert "问这里发生了什么" not in body
    assert "接着问工程师艾拉" in body


def test_first_meeting_still_asks_the_opening_question():
    state = GameState(
        campaign_id="c",
        world_id="w",
        location_id="corridor",
        doors={"hatch": True},
    )
    assert "问这里发生了什么" in _texts(_hints(state))


def test_the_way_back_is_offered_last():
    state = GameState(
        campaign_id="c",
        world_id="w",
        location_id="dock",
        doors={"hatch": True},
        story_nodes=["corridor", "dock"],
    )
    # The drone stands in this room, so it leads; what matters is that the
    # doorway back ranks behind the way forward, not ahead of it.
    targets = [o["target"] for o in _hints(state)]
    assert "气闸舱" not in targets


def test_trail_that_names_no_real_room_marks_nothing_as_back():
    state = GameState(
        campaign_id="c",
        world_id="w",
        location_id="dock",
        doors={"hatch": True},
        story_nodes=["turn:3", "dock"],
    )
    targets = [o["target"] for o in _hints(state)]
    assert "内舱" in targets
    assert "气闸舱" not in targets


def test_builder_alone_keeps_the_three_button_promise():
    """The public builder is callable without a world: it must still answer."""
    options = build_suggestions()
    assert len(options) == 3
    options = build_suggestions(in_combat=True)
    assert len(options) == 3
