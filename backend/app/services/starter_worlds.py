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
}


def _is_zh(language: str) -> bool:
    """True for Chinese output: the UI passes "zh"/"zh-CN", the stores "中文"."""
    value = (language or "").strip().lower()
    return value.startswith("zh") or any("\u4e00" <= ch <= "\u9fff" for ch in value)


def preset_for(world_type: str, language: str) -> Optional[Dict[str, Any]]:
    """Return the authored preset for a world type, or None when unknown."""
    key = _slug(world_type)
    builder = PRESETS.get(key)
    if builder is None:
        # A few aliases map onto the single authored preset for now.
        if key in ("medieval", "rural", "village", "fantasy_village"):
            builder = PRESETS["fantasy"]
        else:
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
    return document


def starter_world_id(campaign_id: str) -> str:
    """Deterministic id so a retry never creates a second world for one campaign."""
    fragment = _slug(campaign_id, 32) or "campaign"
    return f"starter_{fragment}"


def alternate_world_id(world_id: str, attempt: int) -> str:
    """Id for the n-th attempt, so an id clash never overwrites a world."""
    return world_id if attempt <= 0 else f"{world_id}_{attempt}"
