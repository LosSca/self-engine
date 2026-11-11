"""Author a whole playable scenario from one prompt (需求 ①).

The model drafts; this module decides what is *accepted*.  A draft is validated
item by item: the world must pass ``WorldDefinition`` reference validation, every
knowledge entry must pass ``KnowledgeEntryCreate``, the storyline must pass
``validate_storyline``.  Whatever fails is dropped and reported in ``warnings``
instead of aborting the whole authoring run -- a scenario with four good
locations beats no scenario at all, as long as the failure is visible.

Nothing here writes authoritative game state.  ``apply_authored_scenario`` only
creates *configuration* (a scenario, its knowledge, its storyline and a world
document); it never touches a campaign state.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from app.models.knowledge import KnowledgeEntryCreate, Storyline
from app.services import ai_generator, world_service
from app.services.ai_generator import GenerationError

_TIMEOUT = 120.0

SYSTEM = (
    "你是文字 RPG 的剧本作者。只输出 JSON，不要 markdown，不要解释。"
    "你写的是配置数据：地点、物品、门、实体、事件、任务、知识与剧情线。"
    "所有引用必须闭合：出口目标、门钥匙、任务目标、事件效果都要指向这份 JSON 里已有的 id。"
)

_SCHEMA = r'''{
  "world": {
    "name": "...", "description": "...", "start_location_id": "...",
    "locations": [{"id":"snake_case","name":"...","description":"...","region":"...","x":0,"y":0,
      "exits":[{"target_id":"...","label":"...","door_id":null,"requires_item_id":null}],
      "door_ids":[],"item_ids":[],"npc_ids":[]}],
    "items": [{"id":"...","name":"...","description":"...","tags":[],"stackable":true,
      "pickup_requires":[],"use_effects":[],"consumable":false,"slot":"","bonus":{},"price":0}],
    "doors": [{"id":"...","name":"...","location_id":"...","locked":true,"opened":false,
      "key_item_id":"...","consumes_key":false}],
    "entities": [{"id":"...","name":"...","description":"...","location_id":"...",
      "kind":"npc|creature|object","faction":"","dialogue":[],"hostile":false,
      "attributes":{},"items":[],"shop_items":[]}],
    "quests": [{"id":"...","title":"...","description":"...","objectives":[],"rewards":[],
      "enabled":true,"prerequisites":[],"accept_requires":[],"fail_conditions":[],"auto_accept":true}],
    "events": [{"id":"...","name":"...","description":"...","enabled":true,
      "trigger":"action|state_change","trigger_action":"look|take|open|move|talk|use|custom",
      "trigger_target":null,"conditions":[],"effects":[],"priority":0,"repeat":"once",
      "followup_events":[],"failure_events":[],"cooldown_turns":0,"max_fires":null,
      "branches":[],"choices":[]}],
    "custom_actions": [], "choices": []
  },
  "knowledge": [{"layer":"global|story|scene|character|event","title":"...","content":"...",
    "tags":[],"depth":0,"always_on":false,"mode_scope":"both"}],
  "storyline": {"name":"...","description":"...",
    "lines":[{"id":"trunk","title":"主线","kind":"main","description":"","enabled":true}],
    "packs":[],
    "nodes":[{"id":"snake_case","title":"...","kind":"main|branch|side","line":"trunk",
      "summary":"...","prereq":[],"conditions":[],"requires_event":null,
      "requires_event_pack":null,"knowledge_ids":[]}]}
}'''



def build_prompt(prompt: str, language: str, location_count: int = 6) -> str:
    return (
        "根据下面的需求，写出一份完整可玩的剧本配置，语言：" + language + "。"
        + "\n需求：" + prompt
        + "\n\n硬性要求：\n"
        + "1. 至少一条可完成的主任务，任务目标必须指向真实存在的门 / 物品 / 地点 id。\n"
        + "2. 至少一个 NPC，放在起点或起点相邻的地点。\n"
        + "3. 必须有一处「需要钥匙才能通过」的阻碍：门 locked=true 且 key_item_id 指向一个已定义的物品。\n"
        + "4. 故事线的第一个 main 节点没有前置，其余节点由前置串起来。\n"
        + "5. 知识条目要覆盖世界总览、主要地点、关键人物各至少一条。\n"
        + "6. 只写数据，不写代码、不写数值公式。\n\n"
        + "地点数量建议 " + str(location_count) + " 个左右。\n"
        + "输出严格遵循此 JSON 结构：\n" + _SCHEMA
    )


def _clean_world(payload: Any, world_id: str, scenario_id: str):
    """Validate a drafted world; return (world, warnings)."""
    from app.models.game import WorldDefinition

    if not isinstance(payload, dict):
        return None, ["模型没有返回世界对象"]
    data = dict(payload)
    data["world_id"] = world_id
    data["scenario_id"] = scenario_id
    try:
        world = WorldDefinition.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - authored input
        return None, ["世界结构无效：" + str(exc)]
    problems = world.validate_references()
    if problems:
        return None, ["世界引用校验失败：" + "；".join(problems[:8])]
    return world, []


def _clean_knowledge(payload: Any):
    if payload is None:
        return [], []
    if not isinstance(payload, list):
        return [], ["知识条目不是列表，已丢弃"]
    accepted = []
    warnings = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            warnings.append("第 %d 条知识不是对象，已丢弃" % (index + 1))
            continue
        try:
            accepted.append(KnowledgeEntryCreate.model_validate(item))
        except Exception as exc:  # noqa: BLE001 - authored input
            warnings.append("第 %d 条知识未通过校验：%s" % (index + 1, exc))
    return accepted, warnings


def _clean_storyline(payload: Any):
    if payload is None:
        return None, []
    if not isinstance(payload, dict):
        return None, ["剧情线不是对象，已丢弃"]
    try:
        storyline = Storyline.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - authored input
        return None, ["剧情线结构无效：" + str(exc)]
    from app.engines.story_engine import validate_storyline

    # Node knowledge_ids refer to knowledge entry ids that do not exist yet at
    # draft time, so pass None: only structure is checked here.
    problems = validate_storyline(storyline, None, None)
    if problems:
        return None, ["剧情线校验失败：" + "；".join(problems[:8])]
    return storyline, []


async def author_scenario(
    router,
    prompt: str,
    world_id: str,
    scenario_id: str,
    language: str = "中文",
    location_count: int = 6,
) -> Dict[str, Any]:
    """One model call, then item-by-item validation."""
    user = build_prompt(prompt, language, location_count)
    try:
        payload = await asyncio.wait_for(
            ai_generator._ask(router, SYSTEM, user), timeout=_TIMEOUT
        )
    except asyncio.TimeoutError:
        raise GenerationError("生成超时，请稍后重试或换用更快的模型")
    if not isinstance(payload, dict):
        raise GenerationError("模型没有返回剧本对象")

    warnings = []
    world, world_warnings = _clean_world(payload.get("world"), world_id, scenario_id)
    warnings.extend(world_warnings)
    entries, knowledge_warnings = _clean_knowledge(payload.get("knowledge"))
    warnings.extend(knowledge_warnings)
    storyline, storyline_warnings = _clean_storyline(payload.get("storyline"))
    warnings.extend(storyline_warnings)

    if world is None and not entries and storyline is None:
        raise GenerationError(
            "模型返回的剧本没有任何可用部分：" + ("；".join(warnings[:5]) or "空内容")
        )
    return {
        "world": world,
        "knowledge": entries,
        "storyline": storyline,
        "warnings": warnings,
    }


def apply_authored_scenario(
    scenario_store,
    game_store,
    draft: Dict[str, Any],
    scenario_id: str,
    scenario_title: str = "",
    language: str = "中文",
) -> Dict[str, Any]:
    """Persist an accepted draft as configuration. Never touches campaign state."""
    entry_ids = []
    for entry in draft.get("knowledge") or []:
        try:
            stored = scenario_store.add_knowledge(scenario_id, entry)
            entry_ids.append(stored.id)
        except Exception:  # noqa: BLE001 - one bad entry must not lose the rest
            continue

    storyline_saved = False
    storyline = draft.get("storyline")
    if storyline is not None:
        try:
            # The storyline lives inside the play config; rewriting one field must
            # not reset the author's mode or preset choices.
            config = scenario_store.get_play_config(scenario_id)
            config.storyline = storyline
            scenario_store.set_play_config(scenario_id, config)
            storyline_saved = True
        except Exception:  # noqa: BLE001 - reported through the result
            storyline_saved = False

    world_saved = False
    world = draft.get("world")
    if world is not None:
        world_service.save_world(game_store, world)
        world_saved = True

    return {
        "scenario_id": scenario_id,
        "scenario_title": scenario_title,
        "knowledge_ids": entry_ids,
        "storyline_saved": storyline_saved,
        "world_saved": world_saved,
        "world_id": world.world_id if world_saved else "",
        "warnings": list(draft.get("warnings") or []),
        "language": language,
    }
