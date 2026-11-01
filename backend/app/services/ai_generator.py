"""Structured AI drafting for knowledge, storylines and whole worlds.

Every generator asks the model for JSON, validates it with the same Pydantic
models the editors use, and returns drafts. Knowledge/storyline drafts are
never stored automatically: the UI lets the author edit and save them. World
generation validates references and can save the finished document as a new
world, which the editor then opens.

The model is advisory: invalid output is rejected with a readable error and
never reaches the authoritative stores unvalidated.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from app.models.game import WorldDefinition
from app.models.knowledge import (
    KnowledgeEntryCreate,
    Storyline,
)
from app.engines.story_engine import validate_storyline
from app.services import narrator_factory, world_service
from app.services.narrative_service import preset_router

_TIMEOUT = 90.0
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class GenerationError(RuntimeError):
    """The model was unavailable or returned unusable JSON."""


def router_for(preset=None):
    """A router for generation, or None when no model is configured."""
    if preset is not None and getattr(preset, "provider", ""):
        return preset_router(
            {"provider": preset.provider, "model": preset.model}
        )
    if narrator_factory.narrator_choice() != "llm":
        return None
    try:
        return narrator_factory.build_router()
    except Exception:  # noqa: BLE001 - caller reports a friendly error
        return None


def _extract_json(text: str) -> Any:
    raw = (text or "").strip()
    match = _FENCE.search(raw)
    if match:
        raw = match.group(1).strip()
    if not raw:
        raise GenerationError("模型返回了空内容")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = min(
            (index for index in (raw.find("{"), raw.find("[")) if index >= 0),
            default=-1,
        )
        end = max(raw.rfind("}"), raw.rfind("]"))
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise GenerationError("模型返回的不是有效 JSON：" + raw[:200])


async def _ask(router, system: str, user: str) -> Any:
    try:
        text = await asyncio.wait_for(
            router.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=3200,
                temperature=0.6,
            ),
            timeout=_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise GenerationError("生成超时，请稍后重试或换用更快的模型")
    except Exception as exc:  # noqa: BLE001 - report a stable message
        raise GenerationError(f"模型调用失败：{type(exc).__name__}: {exc}")
    return _extract_json(text)


async def generate_knowledge(
    router, prompt: str, count: int = 6, language: str = "中文"
) -> List[Dict[str, Any]]:
    count = max(1, min(20, int(count)))
    system = (
        "你是文字 RPG 的世界观编辑。只输出 JSON，不要 markdown。"
        "条目必须是可验证的事实与氛围，不写游戏规则代码。"
    )
    user = f"""根据下面的需求写 {count} 条知识条目，语言：{language}。
需求：{prompt}

输出 JSON：{{"entries":[{{"layer":"global|story|scene|character|event","title":"...","content":"...","tags":["..."],"depth":0-9,"always_on":false,"mode_scope":"both|storyline|free"}}]}}
要求：标题唯一、内容具体；至少覆盖世界、场景、人物各一条。"""
    payload = await _ask(router, system, user)
    raw_entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(raw_entries, list) or not raw_entries:
        raise GenerationError("模型没有返回知识条目列表")
    drafts = []
    for index, item in enumerate(raw_entries[:count]):
        if not isinstance(item, dict):
            continue
        try:
            drafts.append(KnowledgeEntryCreate.model_validate(item).model_dump())
        except Exception as exc:  # noqa: BLE001 - keep the valid subset
            drafts.append({"_error": f"第 {index + 1} 条无效：{exc}"})
    valid = [item for item in drafts if "_error" not in item]
    if not valid:
        raise GenerationError("模型返回的条目全部未通过校验")
    return drafts


async def generate_storyline(
    router,
    prompt: str,
    node_count: int = 6,
    knowledge_ids: Optional[List[str]] = None,
    event_ids: Optional[List[str]] = None,
    language: str = "中文",
) -> Dict[str, Any]:
    node_count = max(2, min(20, int(node_count)))
    known = knowledge_ids or []
    system = (
        "你是文字 RPG 的剧情设计。只输出 JSON，不要 markdown。"
        "节点推进只能依赖前置节点、条件与已触发事件。"
    )
    user = f"""根据需求写一条剧情线，语言：{language}，约 {node_count} 个节点。
需求：{prompt}
可用知识 id：{known}
可用事件 id：{event_ids or []}

输出 JSON：{{"name":"...","description":"...","lines":[{{"id":"trunk","title":"主线","kind":"main","description":"","enabled":true}}],"nodes":[{{"id":"snake_case","title":"...","kind":"main|branch|side","line":"trunk","summary":"...","prereq":["..."],"conditions":[],"requires_event":null,"knowledge_ids":[]}}]}}
要求：至少一个无前置 main 起点；独立 side 节点必须有条件或 requires_event；所有引用必须存在。"""
    payload = await _ask(router, system, user)
    if not isinstance(payload, dict):
        raise GenerationError("模型没有返回剧情线对象")
    errors = []
    try:
        storyline = Storyline.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - authored input
        raise GenerationError(f"剧情线结构无效：{exc}")
    errors.extend(
        validate_storyline(
            storyline,
            set(known) if knowledge_ids is not None else None,
            set(event_ids) if event_ids is not None else None,
        )
    )
    return {"storyline": storyline.model_dump(mode="json"), "warnings": errors}


async def generate_world(
    router,
    prompt: str,
    scenario_id: str,
    world_id: str,
    language: str = "中文",
) -> Dict[str, Any]:
    system = (
        "你是文字 RPG 的世界编辑。只输出 JSON，不要 markdown。"
        "必须给出闭合的引用：出口、门钥匙、任务目标、事件效果都要指向已存在 id。"
    )
    schema = """{
  "world_id":"...", "scenario_id":"...", "name":"...", "description":"...", "start_location_id":"...",
  "starting_skills":{"stealth":1},
  "locations":[{"id":"...","name":"...","description":"...","region":"...","x":0,"y":0,
    "exits":[{"target_id":"...","label":"...","door_id":null,"requires_item_id":null}],
    "door_ids":[],"item_ids":[],"npc_ids":[]}],
  "items":[{"id":"...","name":"...","description":"...","tags":[],"stackable":true,
    "pickup_requires":[],"use_effects":[],"consumable":false,"slot":"","bonus":{},"price":0}],
  "doors":[{"id":"...","name":"...","location_id":"...","locked":true,"opened":false,"key_item_id":"...","consumes_key":false}],
  "entities":[{"id":"...","name":"...","description":"...","location_id":"...","kind":"npc|creature|object",
    "faction":"","dialogue":[],"hostile":false,"attributes":{},"items":[],"shop_items":[]}],
  "quests":[{"id":"...","title":"...","description":"...","objectives":[],"rewards":[],"enabled":true,
    "prerequisites":[],"accept_requires":[],"fail_conditions":[],"auto_accept":true}],
  "events":[{"id":"...","name":"...","description":"...","enabled":true,"trigger":"action|state_change",
    "trigger_action":"look|take|open|move|talk|use|custom","trigger_target":null,"conditions":[],"effects":[],
    "priority":0,"repeat":"once","followup_events":[],"failure_events":[],"cooldown_turns":0,"max_fires":null,
    "branches":[],"choices":[]}],
  "custom_actions":[], "choices":[]
}"""
    user = f"""生成一个可玩的小型世界，语言：{language}。
需求：{prompt}
硬性要求：4-10 个地点，带 region 与 x/y 网格坐标；至少一条可完成主任务；至少一个 NPC；不要超过 12 个事件。
输出严格遵循此 JSON 结构：
{schema}"""
    payload = await _ask(router, system, user)
    if not isinstance(payload, dict):
        raise GenerationError("模型没有返回世界对象")
    payload["world_id"] = world_id
    payload["scenario_id"] = scenario_id
    try:
        world = WorldDefinition.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - authored input
        raise GenerationError(f"世界结构无效：{exc}")
    errors = world.validate_references()
    if errors:
        raise GenerationError("世界引用校验失败：" + "；".join(errors[:8]))
    return {"world": world, "counting": {
        "locations": len(world.locations),
        "items": len(world.items),
        "entities": len(world.entities),
        "events": len(world.events),
    }}


def save_generated_world(store, world: WorldDefinition) -> Dict[str, Any]:
    return world_service.save_world(store, world)
