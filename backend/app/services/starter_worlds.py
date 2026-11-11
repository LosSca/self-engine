"""Authored starter worlds for campaigns that have no world bound yet.

A campaign created from a scenario with no world used to fall through to the
legacy LLM lane, where the rules engine could not see its state -- two systems
then disagreed about the same player. This module removes that gap: every
campaign can be given a *validated* starter world, either from a small set of
authored presets (picked from the scenario's `world_type`) or from a generic
fallback generated from the scenario's own title and opening text.

Everything here is plain data handed to `validate_world`; nothing executes.
Generation is deterministic: the same scenario always yields the same world,
and an id collision is resolved by a suffix instead of overwriting a world the
author may already have edited.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional



def _slug(text: str, limit: int = 40) -> str:
    """ASCII-safe fragment of arbitrary text (CJK is kept, punctuation dropped)."""
    cleaned = re.sub(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", "_", (text or "").strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:limit]


def _unused_first_line(text: str, limit: int = 160) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _preset_fantasy(language: str) -> Dict[str, Any]:
    zh = (language or "").lower().startswith("zh")
    if zh:
        return {
            "name": "边境村庄",
            "description": "一座依着森林边缘的小村庄，仓库的门锁着，钥匙不在它该在的地方。",
            "start_location_id": "village_square",
            "locations": [
                {
                    "id": "village_square", "name": "村口广场", "region": "village", "x": 0, "y": 1,
                    "description": "夯土的广场中央有一口井，往北是仓库，往东是林间小路。",
                    "tags": ["村庄"],
                    "exits": [
                        {"target_id": "warehouse", "label": "仓库", "door_id": "warehouse_door"},
                        {"target_id": "forest_edge", "label": "林间小路"},
                    ],
                    "item_ids": ["iron_key"],
                },
                {
                    "id": "warehouse", "name": "仓库", "region": "village", "x": 0, "y": 0,
                    "description": "货架上一层薄灰，一袋备用口粮被推到最里侧。",
                    "tags": ["室内"],
                    "exits": [{"target_id": "village_square", "label": "广场", "door_id": "warehouse_door"}],
                    "item_ids": ["trail_ration"],
                    "door_ids": ["warehouse_door"],
                },
                {
                    "id": "forest_edge", "name": "林边", "region": "forest", "x": 1, "y": 1,
                    "description": "树干上留着新鲜的抓痕，雾气在低处流动。",
                    "tags": ["野外"],
                    "exits": [{"target_id": "village_square", "label": "村庄"}],
                    "npc_ids": ["gate_watcher"],
                },
            ],
            "items": [
                {"id": "iron_key", "name": "铁钥匙", "description": "钥匙齿上沾着泥，能打开仓库那扇门。", "tags": ["钥匙"], "stackable": False},
                {"id": "trail_ration", "name": "备用口粮", "description": "用油纸包好的干粮。", "tags": ["补给"], "stackable": True},
            ],
            "doors": [
                {"id": "warehouse_door", "name": "仓库门", "location_id": "village_square", "locked": True, "opened": False, "key_item_id": "iron_key", "consumes_key": False},
            ],
            "entities": [
                {"id": "gate_watcher", "name": "守林人", "kind": "npc", "location_id": "forest_edge", "description": "他靠在树上，似乎在等谁从仓库那边过来。", "dialogue": ["仓库的门可不认生人。"]},
            ],
            "quests": [
                {
                    "id": "q_warehouse", "title": "仓库里的东西",
                    "description": "找到钥匙，把仓库门打开。",
                    "objectives": [
                        {"id": "find_key", "kind": "obtain_item", "target": "iron_key", "description": "取得铁钥匙"},
                        {"id": "open_door", "kind": "open_door", "target": "warehouse_door", "description": "打开仓库门"},
                    ],
                    "rewards": [{"kind": "set_flag", "target": "warehouse_open", "value": True}],
                }
            ],
        }
    return {
        "name": "Border Village",
        "description": "A small village at the edge of the forest. The warehouse door is locked and the key is not where it should be.",
        "start_location_id": "village_square",
        "locations": [
            {
                "id": "village_square", "name": "Village Square", "region": "village", "x": 0, "y": 1,
                "description": "A packed-earth square with a well in the middle. The warehouse is north, a forest path east.",
                "tags": ["village"],
                "exits": [
                    {"target_id": "warehouse", "label": "Warehouse", "door_id": "warehouse_door"},
                    {"target_id": "forest_edge", "label": "Forest Path"},
                ],
                "item_ids": ["iron_key"],
            },
            {
                "id": "warehouse", "name": "Warehouse", "region": "village", "x": 0, "y": 0,
                "description": "Dust on the shelves; a bag of spare rations has been pushed to the back.",
                "tags": ["indoor"],
                "exits": [{"target_id": "village_square", "label": "Square", "door_id": "warehouse_door"}],
                "item_ids": ["trail_ration"],
                "door_ids": ["warehouse_door"],
            },
            {
                "id": "forest_edge", "name": "Forest Edge", "region": "forest", "x": 1, "y": 1,
                "description": "Fresh claw marks on a trunk; mist drifts at knee height.",
                "tags": ["wild"],
                "exits": [{"target_id": "village_square", "label": "Village"}],
                "npc_ids": ["gate_watcher"],
            },
        ],
        "items": [
            {"id": "iron_key", "name": "Iron Key", "description": "Mud in the teeth; it opens the warehouse door.", "tags": ["key"], "stackable": False},
            {"id": "trail_ration", "name": "Spare Ration", "description": "Dry food wrapped in oiled paper.", "tags": ["supply"], "stackable": True},
        ],
        "doors": [
            {"id": "warehouse_door", "name": "Warehouse Door", "location_id": "village_square", "locked": True, "opened": False, "key_item_id": "iron_key", "consumes_key": False},
        ],
        "entities": [
            {"id": "gate_watcher", "name": "Gate Watcher", "kind": "npc", "location_id": "forest_edge", "description": "He leans on a tree, as if waiting for someone to leave the warehouse.", "dialogue": ["That door will not open for strangers."]},
        ],
        "quests": [
            {
                "id": "q_warehouse", "title": "What Is In The Warehouse",
                "description": "Find the key and open the warehouse door.",
                "objectives": [
                    {"id": "find_key", "kind": "obtain_item", "target": "iron_key", "description": "Take the iron key"},
                    {"id": "open_door", "kind": "open_door", "target": "warehouse_door", "description": "Open the warehouse door"},
                ],
                "rewards": [{"kind": "set_flag", "target": "warehouse_open", "value": True}],
            }
        ],
    }


def _preset_orbital(language: str) -> Dict[str, Any]:
    """A station gone quiet: a keycard, a sealed reactor and one engineer."""
    zh = _is_zh(language)
    if zh:
        return {
            "name": "轨道站事故",
            "description": "一座失去联络的轨道站，走廊里只剩应急灯和冷却液的滴答声。",
            "start_location_id": "dock",
            "starting_skills": {"engineering": 1},
            "locations": [
                {
                    "id": "dock", "name": "对接舱", "region": "station", "x": 0, "y": 1,
                    "description": "气闸还未完全闭合，舷窗外是一颗缓慢旋转的星球。",
                    "tags": ["入口"],
                    "exits": [{"target_id": "corridor", "label": "内舱", "door_id": "dock_hatch"}],
                    "door_ids": ["dock_hatch"],
                    "item_ids": ["keycard"],
                },
                {
                    "id": "corridor", "name": "环形走廊", "region": "station", "x": 1, "y": 1,
                    "description": "应急灯每隔三秒闪一次，墙上贴着烧焦的疏散图。",
                    "tags": ["通道"],
                    "exits": [
                        {"target_id": "dock", "label": "气闸", "door_id": "dock_hatch"},
                        {"target_id": "reactor", "label": "反应堆"},
                        {"target_id": "medbay", "label": "医疗舱"},
                    ],
                    "npc_ids": ["engineer"],
                },
                {
                    "id": "reactor", "name": "反应堆", "region": "core", "x": 2, "y": 0,
                    "description": "冷却管发出低鸣，控制台上有一枚红色的手动锁。",
                    "tags": ["危险"],
                    "exits": [{"target_id": "corridor", "label": "走廊"}],
                    "door_ids": ["reactor_door"],
                    "item_ids": ["coolant_cell"],
                },
                {
                    "id": "medbay", "name": "医疗舱", "region": "station", "x": 2, "y": 2,
                    "description": "药柜被撬开，地上散落着空的注射器。",
                    "tags": ["室内"],
                    "exits": [{"target_id": "corridor", "label": "走廊"}],
                    "item_ids": ["medkit"],
                },
            ],
            "items": [
                {"id": "keycard", "name": "主管钥匙卡", "description": "边角被烧黑，仍能刷开反应堆门。", "tags": ["钥匙"], "stackable": False},
                {"id": "coolant_cell", "name": "冷却单元", "description": "沉重的手提式冷却单元。", "tags": ["任务"], "stackable": False},
                {"id": "medkit", "name": "医疗包", "description": "还能用一次。", "tags": ["消耗品"], "stackable": True},
            ],
            "doors": [
                {"id": "dock_hatch", "name": "气闸内门", "location_id": "dock", "locked": True, "opened": False, "key_item_id": "keycard", "consumes_key": False},
                {"id": "reactor_door", "name": "反应堆门", "location_id": "reactor", "locked": True, "opened": False, "key_item_id": "keycard", "consumes_key": False},
            ],
            "entities": [
                {"id": "engineer", "name": "工程师艾拉", "kind": "npc", "location_id": "corridor", "faction": "crew", "description": "手一直按在耳后的通讯器上。", "dialogue": ["别碰反应堆。", "钥匙卡在对接舱，自己拿。"]},
            ],
            "quests": [
                {
                    "id": "stabilize_reactor", "title": "稳定反应堆",
                    "description": "拿到钥匙卡，打开反应堆门，把冷却单元送进去。",
                    "objectives": [
                        {"id": "get_keycard", "kind": "obtain_item", "target": "keycard", "description": "取得主管钥匙卡"},
                        {"id": "open_reactor", "kind": "open_door", "target": "reactor_door", "description": "打开反应堆门"},
                    ],
                    "rewards": [{"kind": "set_flag", "target": "station_safe", "value": True}],
                    "auto_accept": True,
                }
            ],
            "events": [
                {
                    "id": "reactor_alarm", "name": "反应堆警报", "trigger": "state_change", "repeat": "once",
                    "conditions": [{"kind": "has_item", "target": "coolant_cell", "op": "eq", "value": True}],
                    "effects": [
                        {"kind": "adjust_alert", "target": "engineer", "amount": 1},
                        {"kind": "set_flag", "target": "coolant_found", "value": True},
                    ],
                }
            ],
        }
    return {
        "name": "Orbital Station Incident",
        "description": "A station out of contact: emergency lights and coolant dripping in the corridor.",
        "start_location_id": "dock",
        "starting_skills": {"engineering": 1},
        "locations": [
            {
                "id": "dock", "name": "Docking Bay", "region": "station", "x": 0, "y": 1,
                "description": "The airlock never finished closing; a planet turns slowly outside.",
                "tags": ["entrance"],
                "exits": [{"target_id": "corridor", "label": "Inner corridor", "door_id": "dock_hatch"}],
                "door_ids": ["dock_hatch"],
                "item_ids": ["keycard"],
            },
            {
                "id": "corridor", "name": "Ring Corridor", "region": "station", "x": 1, "y": 1,
                "description": "Emergency lights blink every three seconds beside a scorched evacuation plan.",
                "tags": ["passage"],
                "exits": [
                    {"target_id": "dock", "label": "Airlock", "door_id": "dock_hatch"},
                    {"target_id": "reactor", "label": "Reactor"},
                    {"target_id": "medbay", "label": "Medbay"},
                ],
                "npc_ids": ["engineer"],
            },
            {
                "id": "reactor", "name": "Reactor", "region": "core", "x": 2, "y": 0,
                "description": "Coolant pipes hum; a red manual lock sits on the console.",
                "tags": ["danger"],
                "exits": [{"target_id": "corridor", "label": "Corridor"}],
                "door_ids": ["reactor_door"],
                "item_ids": ["coolant_cell"],
            },
            {
                "id": "medbay", "name": "Medbay", "region": "station", "x": 2, "y": 2,
                "description": "The cabinet has been forced open, empty syringes on the floor.",
                "tags": ["indoor"],
                "exits": [{"target_id": "corridor", "label": "Corridor"}],
                "item_ids": ["medkit"],
            },
        ],
        "items": [
            {"id": "keycard", "name": "Supervisor Keycard", "description": "Charred at one corner, still opens the reactor door.", "tags": ["key"], "stackable": False},
            {"id": "coolant_cell", "name": "Coolant Cell", "description": "A heavy portable coolant cell.", "tags": ["quest"], "stackable": False},
            {"id": "medkit", "name": "Medkit", "description": "Good for one more use.", "tags": ["consumable"], "stackable": True},
        ],
        "doors": [
            {"id": "dock_hatch", "name": "Inner Airlock", "location_id": "dock", "locked": True, "opened": False, "key_item_id": "keycard", "consumes_key": False},
            {"id": "reactor_door", "name": "Reactor Door", "location_id": "reactor", "locked": True, "opened": False, "key_item_id": "keycard", "consumes_key": False},
        ],
        "entities": [
            {"id": "engineer", "name": "Engineer Ayla", "kind": "npc", "location_id": "corridor", "faction": "crew", "description": "One hand never leaves the comms unit behind her ear.", "dialogue": ["Do not touch the reactor.", "The keycard is in the docking bay. Take it yourself."]},
        ],
        "quests": [
            {
                "id": "stabilize_reactor", "title": "Stabilize the Reactor",
                "description": "Take the keycard, open the reactor door and get the coolant cell inside.",
                "objectives": [
                    {"id": "get_keycard", "kind": "obtain_item", "target": "keycard", "description": "Take the supervisor keycard"},
                    {"id": "open_reactor", "kind": "open_door", "target": "reactor_door", "description": "Open the reactor door"},
                ],
                "rewards": [{"kind": "set_flag", "target": "station_safe", "value": True}],
                "auto_accept": True,
            }
        ],
        "events": [
            {
                "id": "reactor_alarm", "name": "Reactor Alarm", "trigger": "state_change", "repeat": "once",
                "conditions": [{"kind": "has_item", "target": "coolant_cell", "op": "eq", "value": True}],
                "effects": [
                    {"kind": "adjust_alert", "target": "engineer", "amount": 1},
                    {"kind": "set_flag", "target": "coolant_found", "value": True},
                ],
            }
        ],
    }


def _preset_fog_town(language: str) -> Dict[str, Any]:
    """A closed town, a missing file and a cellar nobody admits to locking."""
    zh = _is_zh(language)
    if zh:
        return {
            "name": "雾镇档案",
            "description": "雾从河面爬进街道，镇上的人都说档案室从来没有钥匙。",
            "start_location_id": "square",
            "locations": [
                {
                    "id": "square", "name": "镇广场", "region": "town", "x": 1, "y": 1,
                    "description": "雾里只剩一盏路灯亮着。",
                    "tags": ["广场"],
                    "exits": [{"target_id": "inn", "label": "旅店"}, {"target_id": "archive", "label": "档案室"}],
                    "npc_ids": ["innkeeper"],
                },
                {
                    "id": "inn", "name": "旅店", "region": "town", "x": 0, "y": 2,
                    "description": "壁炉里的火快熄了，柜台上压着一张旧报纸。",
                    "tags": ["室内"],
                    "exits": [{"target_id": "square", "label": "广场"}],
                    "item_ids": ["brass_key"],
                    "npc_ids": ["innkeeper"],
                },
                {
                    "id": "archive", "name": "档案室", "region": "archive", "x": 2, "y": 1,
                    "description": "书架顶到天花板，最里面有一扇通往地窖的门。",
                    "tags": ["室内"],
                    "exits": [{"target_id": "square", "label": "广场"}, {"target_id": "cellar", "label": "地窖", "door_id": "cellar_door"}],
                    "door_ids": ["cellar_door"],
                },
                {
                    "id": "cellar", "name": "地窖", "region": "archive", "x": 3, "y": 1,
                    "description": "潮湿的石阶尽头，一只铁箱压在旧档案上。",
                    "tags": ["地下室"],
                    "exits": [{"target_id": "archive", "label": "档案室", "door_id": "cellar_door"}],
                    "item_ids": ["missing_file"],
                },
            ],
            "items": [
                {"id": "brass_key", "name": "黄铜钥匙", "description": "钥匙柄上刻着地窖的编号。", "tags": ["钥匙"], "stackable": False},
                {"id": "missing_file", "name": "失踪档案", "description": "记录着十年前最后一班渡船。", "tags": ["线索"], "stackable": False},
            ],
            "doors": [
                {"id": "cellar_door", "name": "地窖门", "location_id": "archive", "locked": True, "opened": False, "key_item_id": "brass_key", "consumes_key": False},
            ],
            "entities": [
                {"id": "innkeeper", "name": "旅店老板", "kind": "npc", "location_id": "inn", "faction": "town", "description": "擦杯子的手一直没停。", "dialogue": ["十年前的渡船？没人记得。", "地窖的钥匙？我从来没拿过。"]},
            ],
            "quests": [
                {
                    "id": "find_file", "title": "找回失踪档案",
                    "description": "拿到黄铜钥匙，打开地窖，把档案带回镇上。",
                    "objectives": [
                        {"id": "take_key", "kind": "obtain_item", "target": "brass_key", "description": "取得黄铜钥匙"},
                        {"id": "open_cellar", "kind": "open_door", "target": "cellar_door", "description": "打开地窖门"},
                    ],
                    "rewards": [{"kind": "set_flag", "target": "truth_found", "value": True}],
                    "auto_accept": True,
                }
            ],
        }
    return {
        "name": "Fog Town Files",
        "description": "Fog climbs in from the river; nobody in town admits the archive ever had a key.",
        "start_location_id": "square",
        "locations": [
            {
                "id": "square", "name": "Town Square", "region": "town", "x": 1, "y": 1,
                "description": "Only one street lamp still burns through the fog.",
                "tags": ["square"],
                "exits": [{"target_id": "inn", "label": "Inn"}, {"target_id": "archive", "label": "Archive"}],
                "npc_ids": ["innkeeper"],
            },
            {
                "id": "inn", "name": "Inn", "region": "town", "x": 0, "y": 2,
                "description": "The hearth is dying; an old newspaper is weighed down on the counter.",
                "tags": ["indoor"],
                "exits": [{"target_id": "square", "label": "Square"}],
                "item_ids": ["brass_key"],
                "npc_ids": ["innkeeper"],
            },
            {
                "id": "archive", "name": "Archive", "region": "archive", "x": 2, "y": 1,
                "description": "Shelves reach the ceiling; at the back a door leads down to the cellar.",
                "tags": ["indoor"],
                "exits": [{"target_id": "square", "label": "Square"}, {"target_id": "cellar", "label": "Cellar", "door_id": "cellar_door"}],
                "door_ids": ["cellar_door"],
            },
            {
                "id": "cellar", "name": "Cellar", "region": "archive", "x": 3, "y": 1,
                "description": "At the end of the damp steps an iron box sits on old files.",
                "tags": ["basement"],
                "exits": [{"target_id": "archive", "label": "Archive", "door_id": "cellar_door"}],
                "item_ids": ["missing_file"],
            },
        ],
        "items": [
            {"id": "brass_key", "name": "Brass Key", "description": "The cellar number is cut into the bow.", "tags": ["key"], "stackable": False},
            {"id": "missing_file", "name": "Missing File", "description": "It records the last ferry, ten years ago.", "tags": ["clue"], "stackable": False},
        ],
        "doors": [
            {"id": "cellar_door", "name": "Cellar Door", "location_id": "archive", "locked": True, "opened": False, "key_item_id": "brass_key", "consumes_key": False},
        ],
        "entities": [
            {"id": "innkeeper", "name": "Innkeeper", "kind": "npc", "location_id": "inn", "faction": "town", "description": "She never stops wiping the same glass.", "dialogue": ["The ferry, ten years back? Nobody remembers.", "The cellar key? I never held it."]},
        ],
        "quests": [
            {
                "id": "find_file", "title": "Find the Missing File",
                "description": "Take the brass key, open the cellar and bring the file back to town.",
                "objectives": [
                    {"id": "take_key", "kind": "obtain_item", "target": "brass_key", "description": "Take the brass key"},
                    {"id": "open_cellar", "kind": "open_door", "target": "cellar_door", "description": "Open the cellar door"},
                ],
                "rewards": [{"kind": "set_flag", "target": "truth_found", "value": True}],
                "auto_accept": True,
            }
        ],
    }


def _preset_wild_camp(language: str) -> Dict[str, Any]:
    """A river valley: a camp, a hungry wolf and a supply cache worth reaching."""
    zh = _is_zh(language)
    if zh:
        return {
            "name": "荒野营地",
            "description": "补给只够两天，河谷对岸的遗迹在雨里发黑。",
            "start_location_id": "camp",
            "starting_skills": {"survival": 2},
            "locations": [
                {
                    "id": "camp", "name": "营地", "region": "river", "x": 0, "y": 1,
                    "description": "帐篷被风吹得猎猎作响，篝火剩下一点红。",
                    "tags": ["营地"],
                    "exits": [{"target_id": "river", "label": "河岸"}],
                    "item_ids": ["rope"],
                },
                {
                    "id": "river", "name": "河岸", "region": "river", "x": 1, "y": 1,
                    "description": "水声很大，浅滩上有一串新鲜的爪印。",
                    "tags": ["野外"],
                    "exits": [{"target_id": "camp", "label": "营地"}, {"target_id": "cave", "label": "洞窟"}],
                    "npc_ids": ["wolf"],
                },
                {
                    "id": "cave", "name": "洞窟", "region": "ruins", "x": 2, "y": 1,
                    "description": "越往里越冷，地上有拖拽的痕迹。",
                    "tags": ["危险"],
                    "exits": [{"target_id": "river", "label": "河岸"}],
                    "item_ids": ["supply_cache"],
                },
            ],
            "items": [
                {"id": "rope", "name": "绳索", "description": "还能承受一个人的重量。", "tags": ["工具"], "stackable": True, "price": 3},
                {"id": "supply_cache", "name": "补给箱", "description": "上一支队伍留下的补给。", "tags": ["补给"], "stackable": True},
            ],
            "doors": [],
            "entities": [
                {"id": "wolf", "name": "灰狼", "kind": "creature", "location_id": "river", "faction": "wild", "hostile": True, "description": "瘦得肋骨分明，却没有退。", "attributes": {"max_hp": 16, "attack": 4, "defense": 1}},
            ],
            "quests": [
                {
                    "id": "survive_night", "title": "熬过今夜",
                    "description": "河谷里有一只没退的狼，洞窟里有上一支队伍留下的补给。",
                    "objectives": [
                        {"id": "get_supplies", "kind": "obtain_item", "target": "supply_cache", "description": "取得补给箱"},
                    ],
                    "rewards": [{"kind": "add_xp", "amount": 60}],
                    "auto_accept": True,
                }
            ],
            "events": [
                {
                    "id": "wolf_alert", "name": "狼的警戒", "trigger": "state_change", "repeat": "once",
                    "conditions": [{"kind": "entity_alert", "target": "wolf", "op": "gte", "count": 1}],
                    "effects": [{"kind": "set_flag", "target": "wolf_angry", "value": True}],
                }
            ],
        }
    return {
        "name": "Wild Camp",
        "description": "Two days of supplies left; the ruins across the river are going black in the rain.",
        "start_location_id": "camp",
        "starting_skills": {"survival": 2},
        "locations": [
            {
                "id": "camp", "name": "Camp", "region": "river", "x": 0, "y": 1,
                "description": "The tent snaps in the wind; the fire is down to a red core.",
                "tags": ["camp"],
                "exits": [{"target_id": "river", "label": "Riverbank"}],
                "item_ids": ["rope"],
            },
            {
                "id": "river", "name": "Riverbank", "region": "river", "x": 1, "y": 1,
                "description": "The water is loud; fresh claw marks cross the shallows.",
                "tags": ["wild"],
                "exits": [{"target_id": "camp", "label": "Camp"}, {"target_id": "cave", "label": "Cave"}],
                "npc_ids": ["wolf"],
            },
            {
                "id": "cave", "name": "Cave", "region": "ruins", "x": 2, "y": 1,
                "description": "Colder further in, with drag marks on the floor.",
                "tags": ["danger"],
                "exits": [{"target_id": "river", "label": "Riverbank"}],
                "item_ids": ["supply_cache"],
            },
        ],
        "items": [
            {"id": "rope", "name": "Rope", "description": "Still holds one person.", "tags": ["tool"], "stackable": True, "price": 3},
            {"id": "supply_cache", "name": "Supply Cache", "description": "Left behind by the last party.", "tags": ["supply"], "stackable": True},
        ],
        "doors": [],
        "entities": [
            {"id": "wolf", "name": "Grey Wolf", "kind": "creature", "location_id": "river", "faction": "wild", "hostile": True, "description": "Thin enough to count its ribs, and it has not backed off.", "attributes": {"max_hp": 16, "attack": 4, "defense": 1}},
        ],
        "quests": [
            {
                "id": "survive_night", "title": "Survive the Night",
                "description": "A wolf that will not back off, and a cache the last party left in the cave.",
                "objectives": [
                    {"id": "get_supplies", "kind": "obtain_item", "target": "supply_cache", "description": "Take the supply cache"},
                ],
                "rewards": [{"kind": "add_xp", "amount": 60}],
                "auto_accept": True,
            }
        ],
        "events": [
            {
                "id": "wolf_alert", "name": "The Wolf Notices", "trigger": "state_change", "repeat": "once",
                "conditions": [{"kind": "entity_alert", "target": "wolf", "op": "gte", "count": 1}],
                "effects": [{"kind": "set_flag", "target": "wolf_angry", "value": True}],
            }
        ],
    }


_GENERIC_ZH = {
    "name": "{title}",
    "description": "{summary}",
    "start_location_id": "starting_point",
    "locations": [
        {
            "id": "starting_point", "name": "落脚处", "region": "start", "x": 0, "y": 1,
            "description": "你第一次真正看清这个地方。风从远处来，脚下是一条通向里侧的路。",
            "tags": ["起点"],
            "exits": [{"target_id": "inner_reach", "label": "往里去", "door_id": "inner_door"}],
            "item_ids": ["traveler_key"],
            "door_ids": ["inner_door"],
        },
        {
            "id": "inner_reach", "name": "里侧", "region": "core", "x": 1, "y": 1,
            "description": "越往里走，越能感觉到有人在看着你。一道门挡住了剩下的路。",
            "tags": ["深处"],
            "exits": [{"target_id": "starting_point", "label": "回到落脚处", "door_id": "inner_door"}],
        },
        {
            "id": "side_path", "name": "旁路", "region": "core", "x": 1, "y": 0,
            "description": "一条几乎被落叶埋掉的小路，有人在这里休息过。",
            "tags": ["野外"],
            "exits": [{"target_id": "inner_reach", "label": "回到里侧", "door_id": "inner_door"}],
            "item_ids": ["field_ration"],
        },
    ],
    "items": [
        {"id": "traveler_key", "name": "旅人的钥匙", "description": "有人把它留在了你最先到的地方。", "tags": ["钥匙"], "stackable": False},
        {"id": "field_ration", "name": "行囊口粮", "description": "还能吃。", "tags": ["补给"], "stackable": True},
    ],
    "doors": [
        {"id": "inner_door", "name": "倒落的门", "location_id": "starting_point", "locked": True, "opened": False, "key_item_id": "traveler_key", "consumes_key": False},
    ],
    "entities": [],
    "quests": [
        {
            "id": "q_first_steps", "title": "第一步",
            "description": "在这片地方站稳，并打开挡路的那扇门。",
            "objectives": [
                {"id": "find_key", "kind": "obtain_item", "target": "traveler_key", "description": "拿到旅人的钥匙"},
                {"id": "open_door", "kind": "open_door", "target": "inner_door", "description": "打开倒落的门"},
            ],
            "rewards": [{"kind": "set_flag", "target": "first_door_open", "value": True}],
        }
    ],
}

_GENERIC_EN = {
    "name": "{title}",
    "description": "{summary}",
    "start_location_id": "starting_point",
    "locations": [
        {
            "id": "starting_point", "name": "The Landing", "region": "start", "x": 0, "y": 1,
            "description": "The first place you really see clearly. Wind from somewhere further in; one path leads on.",
            "tags": ["start"],
            "exits": [{"target_id": "inner_reach", "label": "Inward", "door_id": "inner_door"}],
            "item_ids": ["traveler_key"],
            "door_ids": ["inner_door"],
        },
        {
            "id": "inner_reach", "name": "The Inner Reach", "region": "core", "x": 1, "y": 1,
            "description": "Further in, you feel watched. A door blocks the rest of the way.",
            "tags": ["deep"],
            "exits": [{"target_id": "starting_point", "label": "Back to the landing", "door_id": "inner_door"}],
        },
        {
            "id": "side_path", "name": "Side Path", "region": "core", "x": 1, "y": 0,
            "description": "A path almost buried in fallen leaves; somebody rested here.",
            "tags": ["wild"],
            "exits": [{"target_id": "inner_reach", "label": "Back inside", "door_id": "inner_door"}],
            "item_ids": ["field_ration"],
        },
    ],
    "items": [
        {"id": "traveler_key", "name": "Traveler's Key", "description": "Left where you first arrived.", "tags": ["key"], "stackable": False},
        {"id": "field_ration", "name": "Field Ration", "description": "Still edible.", "tags": ["supply"], "stackable": True},
    ],
    "doors": [
        {"id": "inner_door", "name": "Fallen Gate", "location_id": "starting_point", "locked": True, "opened": False, "key_item_id": "traveler_key", "consumes_key": False},
    ],
    "entities": [],
    "quests": [
        {
            "id": "q_first_steps", "title": "First Steps",
            "description": "Get your footing here and open the door in the way.",
            "objectives": [
                {"id": "find_key", "kind": "obtain_item", "target": "traveler_key", "description": "Take the traveler's key"},
                {"id": "open_door", "kind": "open_door", "target": "inner_door", "description": "Open the fallen gate"},
            ],
            "rewards": [{"kind": "set_flag", "target": "first_door_open", "value": True}],
        }
    ],
}

#: Authored presets, keyed by the scenario's `world_type`.
PRESETS = {
    "fantasy": _preset_fantasy,
    "orbital_station": _preset_orbital,
    "fog_town": _preset_fog_town,
    "wild_camp": _preset_wild_camp,
}

#: Alternative spellings authors and generators actually use for a world type.
#: Keys are already slugged (`_slug` lowercases and turns punctuation into _).
PRESET_ALIASES = {
    "fantasy": ("medieval", "rural", "village", "fantasy_village", "village_fantasy"),
    "orbital_station": (
        "sci_fi", "scifi", "space", "space_station", "orbital", "station", "station_incident",
        "orbital_station_incident", "sci_fi_station", "科幻", "空间站", "太空站", "星际",
    ),
    "fog_town": ("mystery", "noir", "town", "fog", "fogtown", "detective", "悬疑", "雾镇", "小镇"),
    "wild_camp": ("wilderness", "wild", "survival", "outdoor", "camp", "荒野", "生存", "野外"),
}


def preset_keys() -> List[str]:
    """The `world_type` values that have an authored preset (for the UI/API)."""
    return sorted(PRESETS)


def _is_zh(language: str) -> bool:
    """True for Chinese output: the UI passes "zh"/"zh-CN", the stores "中文"."""
    value = (language or "").strip().lower()
    return value.startswith("zh") or any("\u4e00" <= ch <= "\u9fff" for ch in value)


def preset_for(world_type: str, language: str) -> Optional[Dict[str, Any]]:
    """Return the authored preset for a world type, or None when unknown.

    Matching is exact on the slugged type, then through `PRESET_ALIASES`, then by
    substring ("orbital station accident" finds `orbital_station`). Unknown types
    return None so the caller falls back to the generic world.
    """
    key = _slug(world_type)
    if not key:
        return None
    # CJK types are not ASCII-slugged away, but an author writing "科幻 空间站"
    # would otherwise miss the alias table: test the raw text as well.
    raw = (world_type or "").strip()
    builder = PRESETS.get(key)
    if builder is None:
        for canonical, aliases in PRESET_ALIASES.items():
            if key in aliases or key.startswith(canonical) or canonical in key:
                builder = PRESETS[canonical]
                break
    if builder is None:
        for canonical in PRESETS:
            if canonical in key:
                builder = PRESETS[canonical]
                break
    if builder is None and raw:
        for canonical, aliases in PRESET_ALIASES.items():
            if any(alias in raw for alias in aliases):
                builder = PRESETS[canonical]
                break
    if builder is None:
        return None
    return builder(language)


def generic_world(title: str, summary: str = "", language: str = "") -> Dict[str, Any]:
    """Build a small, always-valid world from the scenario's own text."""
    template = _GENERIC_ZH if _is_zh(language) else _GENERIC_EN
    data = {key: (value.copy() if isinstance(value, dict) else _deep(value)) for key, value in template.items()}
    fallback_name = "未命名世界" if _is_zh(language) else "Untitled World"
    resolved_title = (title or "").strip() or fallback_name
    resolved_summary = (summary or "").strip() or (f"{resolved_title}，一切都还没有名字。" if _is_zh(language) else f"{resolved_title}. Nothing here has a name yet.")
    data["name"] = resolved_title[:200]
    data["description"] = resolved_summary[:1000]
    return data


def _deep(value):
    if isinstance(value, dict):
        return {k: _deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep(v) for v in value]
    return value


def build_world_document(
    scenario_id: str,
    world_id: str,
    title: str = "",
    summary: str = "",
    language: str = "",
    world_type: str = "",
) -> Dict[str, Any]:
    """Assemble a complete WorldDefinition payload for a scenario."""
    data = preset_for(world_type, language) or generic_world(title, summary, language)
    document = _deep(data)
    document["world_id"] = world_id
    document["scenario_id"] = scenario_id or ""
    #: The description language travels with the world so deterministic prose
    #: (the fallback narrator, effect sentences) can match what the player reads.
    document["language"] = "zh" if _is_zh(language) else "en"
    return document


def starter_world_id(campaign_id: str) -> str:
    """Deterministic id so a retry never creates a second world for one campaign."""
    fragment = _slug(campaign_id, 32) or "campaign"
    return f"starter_{fragment}"


def alternate_world_id(world_id: str, attempt: int) -> str:
    """Id for the n-th attempt, so an id clash never overwrites a world."""
    return world_id if attempt <= 0 else f"{world_id}_{attempt}"
