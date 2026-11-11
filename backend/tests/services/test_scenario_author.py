import json

import pytest

from app.services import scenario_author


WORLD = {
    "name": "雾港",
    "description": "一座被雾笼罩的港口小城。",
    "start_location_id": "harbor",
    "locations": [
        {
            "id": "harbor",
            "name": "港口",
            "description": "栈桥尽头有一盏灯。",
            "region": "海岸",
            "x": 0,
            "y": 0,
            "exits": [{"target_id": "warehouse", "label": "走向仓库", "door_id": "warehouse_door"}],
            "door_ids": ["warehouse_door"],
            "item_ids": ["brass_key"],
            "npc_ids": ["keeper"],
        },
        {
            "id": "warehouse",
            "name": "仓库",
            "description": "货箱堆到屋顶。",
            "region": "海岸",
            "x": 1,
            "y": 0,
            "exits": [{"target_id": "harbor", "label": "回到港口"}],
            "door_ids": [],
            "item_ids": [],
            "npc_ids": [],
        },
    ],
    "items": [
        {
            "id": "brass_key",
            "name": "铜钥匙",
            "description": "钥匙齿上刻着货号。",
            "tags": ["key"],
            "stackable": False,
        }
    ],
    "doors": [
        {
            "id": "warehouse_door",
            "name": "仓库铁门",
            "location_id": "harbor",
            "locked": True,
            "opened": False,
            "key_item_id": "brass_key",
            "consumes_key": False,
        }
    ],
    "entities": [
        {
            "id": "keeper",
            "name": "守夜人",
            "description": "他不看海。",
            "location_id": "harbor",
            "kind": "npc",
            "dialogue": [],
        }
    ],
    "quests": [
        {
            "id": "open_warehouse",
            "title": "打开仓库",
            "description": "找到钥匙。",
            "objectives": [],
            "rewards": [],
        }
    ],
    "events": [],
    "custom_actions": [],
    "choices": [],
}

KNOWLEDGE = [
    {"layer": "global", "title": "雾港", "content": "雾从不停。", "depth": 0, "always_on": True},
    {"layer": "character", "title": "守夜人", "content": "他只数灯。", "depth": 2},
]

STORYLINE = {
    "name": "主线",
    "description": "",
    "lines": [{"id": "trunk", "title": "主线", "kind": "main", "description": "", "enabled": True}],
    "packs": [],
    "nodes": [
        {"id": "arrive", "title": "抵达", "kind": "main", "line": "trunk", "summary": "下船。"},
        {"id": "open_door", "title": "开门", "kind": "main", "line": "trunk", "summary": "进仓库。", "prereq": ["arrive"]},
    ],
}


def _payload(**overrides):
    data = {"world": WORLD, "knowledge": KNOWLEDGE, "storyline": STORYLINE}
    data.update(overrides)
    return data


def test_build_prompt_states_the_required_shape():
    text = scenario_author.build_prompt("一个雾中的港口", "中文", 5)
    assert "一个雾中的港口" in text
    assert "key_item_id" in text
    assert "5" in text


def test_clean_world_accepts_a_closed_reference_set():
    world, warnings = scenario_author._clean_world(WORLD, "w1", "s1")
    assert warnings == []
    assert world.world_id == "w1"
    assert world.scenario_id == "s1"


def test_clean_world_rejects_a_dangling_key():
    broken = json.loads(json.dumps(WORLD))
    broken["doors"][0]["key_item_id"] = "ghost_key"
    world, warnings = scenario_author._clean_world(broken, "w1", "s1")
    assert world is None
    assert warnings and "校验失败" in warnings[0]


def test_clean_world_reports_a_non_object():
    world, warnings = scenario_author._clean_world([], "w1", "s1")
    assert world is None
    assert warnings == ["模型没有返回世界对象"]


def test_clean_knowledge_keeps_the_valid_subset():
    entries, warnings = scenario_author._clean_knowledge(
        [KNOWLEDGE[0], {"layer": "nonsense", "title": "x"}, "junk"]
    )
    assert len(entries) == 1
    assert len(warnings) == 2


def test_clean_knowledge_tolerates_a_missing_section():
    assert scenario_author._clean_knowledge(None) == ([], [])


def test_clean_storyline_rejects_a_dangling_prereq():
    broken = json.loads(json.dumps(STORYLINE))
    broken["nodes"][1]["prereq"] = ["nowhere"]
    storyline, warnings = scenario_author._clean_storyline(broken)
    assert storyline is None
    assert warnings and "剧情线校验失败" in warnings[0]


def test_clean_storyline_accepts_a_chain():
    storyline, warnings = scenario_author._clean_storyline(STORYLINE)
    assert warnings == []
    assert len(storyline.nodes) == 2


class _FakeRouter:
    def __init__(self, payload):
        self.payload = payload

    async def complete(self, messages, max_tokens=0, temperature=0.0):
        return json.dumps(self.payload, ensure_ascii=False)


@pytest.mark.asyncio
async def test_author_scenario_keeps_valid_parts_and_reports_the_rest():
    payload = _payload(knowledge=[KNOWLEDGE[0], {"layer": "bad", "title": "x"}])
    draft = await scenario_author.author_scenario(
        _FakeRouter(payload), "雾港", "w1", "s1"
    )
    assert draft["world"] is not None
    assert len(draft["knowledge"]) == 1
    assert draft["storyline"] is not None
    assert draft["warnings"]


@pytest.mark.asyncio
async def test_author_scenario_fails_when_nothing_survives_validation():
    from app.services.ai_generator import GenerationError

    payload = {"world": {"name": ""}, "knowledge": "nope", "storyline": 42}
    with pytest.raises(GenerationError):
        await scenario_author.author_scenario(_FakeRouter(payload), "x", "w1", "s1")
