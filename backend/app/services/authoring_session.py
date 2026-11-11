"""Conversational completion of a scenario (需求 ③).

A session is a chat transcript that proposes *configuration edits*.  The model
never edits anything: it proposes, a deterministic planner stages the proposal,
and only an explicit commit writes it.  Two consequences matter:

* Nothing reaches the authoritative state even after a commit -- this lane only
  edits the world document, knowledge entries and the storyline.
* A staged proposal is a normal chat message, so an author can look at exactly
  what would change before saying yes.
* Editing the world document goes through ``world_editor`` (validated
  read-modify-write), so a proposal that would break a reference is rejected
  with the reference error rather than saved.
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any, Dict, List, Optional, Tuple

from app.models.game import DoorDef, EntityDef, GameEventDef, ItemDef, LocationDef, QuestDef
from app.models.knowledge import KnowledgeEntryCreate
from app.services import ai_generator, world_editor, world_service
from app.services.ai_generator import GenerationError

_TIMEOUT = 120.0
MAX_HISTORY = 40
MAX_BATCH = 40
OP_KINDS = (
    "add_location",
    "add_item",
    "add_door",
    "add_entity",
    "add_event",
    "add_quest",
    "add_knowledge",
    "set_note",
)

SYSTEM = (
    "你是文字 RPG 的世界编辑助手。玩家会不断提出新想法，你要把它变成可执行的编辑操作。"
    "只输出 JSON，不要 markdown，不要解释。你只能提出操作，不能宣布改动已完成。"
    "所有 id 必须是 snake_case，且不能与现有 id 重复；引用必须指向这个世界里已有或本次新增的 id。"
)
_SCHEMA = """{
  "reply": "给玩家看的一句话说明",
  "ops": [
    {"kind":"add_location","data":{"id":"...","name":"...","description":"...",
     "exits":[{"target_id":"..."}],"item_ids":[],"npc_ids":[]}},
    {"kind":"add_item","data":{"id":"...","name":"...","description":"...","tags":[]}},
    {"kind":"add_door","data":{"id":"...","name":"...","location_id":"...","locked":true,
     "key_item_id":"..."}},
    {"kind":"add_entity","data":{"id":"...","name":"...","location_id":"...","kind":"npc"}},
    {"kind":"add_event","data":{"id":"...","name":"...","trigger":"action",
     "trigger_action":"look","effects":[]}},
    {"kind":"add_quest","data":{"id":"...","title":"...","objectives":[],"rewards":[]}},
    {"kind":"add_knowledge","data":{"layer":"character","title":"...","content":"..."}},
    {"kind":"set_note","data":{"title":"...","content":"..."}}
  ]
}"""


def _catalogue(world, knowledge: List[Any]) -> str:
    """A compact inventory of what already exists, so the model stops inventing ids."""
    lines = ["地点：" + ", ".join(loc.id for loc in world.locations)]
    lines.append("物品：" + (", ".join(item.id for item in world.items) or "无"))
    lines.append("门：" + (", ".join(door.id for door in world.doors) or "无"))
    lines.append("实体：" + (", ".join(ent.id for ent in world.entities) or "无"))
    lines.append("事件：" + (", ".join(ev.id for ev in world.events) or "无"))
    lines.append("任务：" + (", ".join(q.id for q in world.quests) or "无"))
    titles = [getattr(entry, "title", "") for entry in knowledge][:30]
    lines.append("已有知识标题：" + (", ".join(t for t in titles if t) or "无"))
    return "\n".join(lines)


def build_prompt(message: str, world, knowledge: List[Any], history: List[Dict[str, str]], language: str = "中文") -> str:
    transcript = []
    for turn in history[-12:]:
        speaker = "玩家" if turn.get("role") == "user" else "助手"
        transcript.append(speaker + "：" + str(turn.get("text") or ""))
    return (
        "当前世界的已有内容：\n" + _catalogue(world, knowledge)
        + "\n\n最近的对话：\n" + ("\n".join(transcript) or "（尚无）")
        + "\n\n玩家这次说：" + message
        + "\n\n请返回 JSON：reply 是一句中文回复，ops 是要新增的内容（可以为空数组）。"
        + "语言：" + language
        + "\n输出严格遵循此结构：\n" + _SCHEMA
    )


def _matches(payload: Any) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """Split a model payload into a readable reply and validated operations."""
    if not isinstance(payload, dict):
        return "", [], ["模型没有返回对象"]
    reply = str(payload.get("reply") or "").strip()
    raw_ops = payload.get("ops");
    if raw_ops is None:
        return reply, [], []
    if not isinstance(raw_ops, list):
        return reply, [], ["ops 不是列表，已丢弃"]
    warnings: List[str] = []
    ops: List[Dict[str, Any]] = []
    if len(raw_ops) > MAX_BATCH:
        warnings.append("一次给出了 %d 个操作，只采用前 %d 个" % (len(raw_ops), MAX_BATCH))
    for index, item in enumerate(raw_ops[:MAX_BATCH]):
        if not isinstance(item, dict):
            warnings.append("第 %d 个操作不是对象，已丢弃" % (index + 1))
            continue
        kind = str(item.get("kind") or "")
        if kind not in OP_KINDS:
            warnings.append("未知操作 %s，已丢弃" % (kind or "（空）"))
            continue
        data = item.get("data");
        if not isinstance(data, dict):
            warnings.append("%s 缺少 data，已丢弃" % kind)
            continue
        ops.append({"kind": kind, "data": data})
    return reply, ops, warnings

def stage_ops(world, ops: List[Dict[str, Any]]) -> Tuple[Any, List[Dict[str, Any]], List[str]]:
    """Apply operations to a COPY of the world and collect readable warnings.

    Each block re-validates the whole world document, so a proposal that would
    leave a dangling reference is refused here, before the author is asked to
    approve anything.
    """
    draft = copy.deepcopy(world)
    applied: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for op in ops:
        kind = op.get("kind")
        data = op.get("data") or {}
        try:
            draft = _apply_op(draft, kind, data)
        except Exception as exc:  # noqa: BLE001 - authored input
            warnings.append("%s 未通过校验：%s" % (kind, exc))
            continue
        applied.append({"kind": kind, "summary": _summarise(kind, data)})
    return draft, applied, warnings


def _summarise(kind: str, data: Dict[str, Any]) -> str:
    label = str(data.get("name") or data.get("title") or data.get("id") or "")
    if kind == "add_knowledge":
        return "知识：" + label
    if kind == "set_note":
        return "设定：" + label
    return label


def _require_id(data: Dict[str, Any]) -> str:
    identifier = str(data.get("id") or "").strip()
    if not identifier:
        raise ValueError("缺少 id")
    return identifier


def _known(model, field_name: str, identifier: str) -> bool:
    return bool(getattr(model, field_name, None) and any(
        marker == identifier for marker in [getattr(x, "id", "") for x in getattr(model, field_name)]
    ))


def _apply_op(world, kind: str, data: Dict[str, Any]):
    if kind == "add_location":
        return world_editor.upsert_location(
            world, LocationDef.model_validate(_with_id(world, "locations", data))
        )
    if kind == "add_item":
        return world_editor.upsert_item(world, ItemDef.model_validate(_with_id(world, "items", data)))
    if kind == "add_door":
        return world_editor.upsert_door(world, DoorDef.model_validate(_with_id(world, "doors", data)))
    if kind == "add_entity":
        return world_editor.upsert_entity(
            world, EntityDef.model_validate(_with_id(world, "entities", data))
        )
    if kind == "add_event":
        return world_editor.upsert_event(
            world, GameEventDef.model_validate(_with_id(world, "events", data))
        )
    if kind == "add_quest":
        return world_editor.upsert_quest(
            world, QuestDef.model_validate(_with_id(world, "quests", data))
        )
    raise ValueError("未知操作 %s" % kind)


def _with_id(world, field_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one operation payload into a model payload with a free id."""
    payload = dict(data)
    identifier = _require_id(payload)
    if any(getattr(x, "id", "") == identifier for x in getattr(world, field_name)):
        raise ValueError("%s 已存在" % identifier)
    payload["id"] = identifier
    return payload


def plan_knowledge(payloads: List[Dict[str, Any]], existing: List[Any]) -> Tuple[List[Any], List[str]]:
    """Validate proposed knowledge entries and drop ones that already exist."""
    seen = {str(getattr(entry, "title", "")).strip() for entry in existing}
    accepted: List[Any] = []
    warnings: List[str] = []
    for index, payload in enumerate(payloads):
        try:
            entry = KnowledgeEntryCreate.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - authored input
            warnings.append("第 %d 条知识未通过校验：%s" % (index + 1, exc))
            continue
        if entry.title.strip() in seen:
            warnings.append("知识「%s」已存在，已跳过" % entry.title)
            continue
        seen.add(entry.title.strip())
        accepted.append(entry)
    return accepted, warnings

def propose(payload: Any, world, existing: List[Any]) -> Tuple[str, Dict[str, Any]]:
    """Turn a model payload into a reviewable proposal; nothing is persisted."""
    reply, ops, warnings = _matches(payload)
    world_ops = [op for op in ops if op["kind"] not in ("add_knowledge", "set_note")]
    knowledge_ops = [op["data"] for op in ops if op["kind"] == "add_knowledge"]
    notes = [op["data"] for op in ops if op["kind"] == "set_note"]
    draft = None
    applied: List[Dict[str, Any]] = []
    if world_ops:
        draft, applied, world_warnings = stage_ops(world, world_ops)
        warnings.extend(world_warnings)
    else:
        draft = copy.deepcopy(world)
    world_summary = _describe_world(draft)
    changes = _diff_world(world, draft)
    for note in notes:
        title = str(note.get("title") or "note").strip()
        body = str(note.get("content") or "").strip()
        if body:
            knowledge_ops.append(
                {"layer": "global", "title": title[:200], "content": body, "tags": ["authored"]}
            )
    accepted_knowledge, knowledge_warnings = plan_knowledge(knowledge_ops, existing)
    warnings.extend(knowledge_warnings)
    ops_out = applied + [
        {"kind": "add_knowledge", "summary": "知识：" + entry.title} for entry in accepted_knowledge
    ]
    proposal: Dict[str, Any] = {
        "world": draft,
        "world_summary": world_summary,
        "changes": changes,
        "knowledge": accepted_knowledge,
        "knowledge_titles": [entry.title for entry in accepted_knowledge],
        "ops": ops_out,
        "warnings": warnings,
        "empty": not ops_out,
    }
    return reply, proposal


def _describe_world(world) -> Dict[str, Any]:
    return {
        "locations": len(world.locations),
        "items": len(world.items),
        "doors": len(world.doors),
        "entities": len(world.entities),
        "events": len(world.events),
        "quests": len(world.quests),
    }


_COUNTERS = (
    ("locations", "地点"),
    ("items", "物品"),
    ("doors", "门"),
    ("entities", "实体"),
    ("events", "事件"),
    ("quests", "任务"),
)


def _diff_world(before, after) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for field_name, label in _COUNTERS:
        old = len(getattr(before, field_name))
        new = len(getattr(after, field_name))
        if new != old:
            rows.append({"label": label, "before": old, "after": new})
    return rows

async def propose_edits(
    router, message: str, world, existing: List[Any], history: Optional[List[Dict[str, str]]] = None,
    language: str = "中文",
) -> Tuple[str, Dict[str, Any]]:
    """Ask the model for edit operations, then stage them. Never raises on bad output."""
    user = build_prompt(message, world, existing, history or [], language)
    warnings: List[str] = []
    payload: Any = None
    try:
        payload = await asyncio.wait_for(
            ai_generator._ask(router, SYSTEM, user), timeout=_TIMEOUT
        )
    except asyncio.TimeoutError:
        warnings.append("模型超时，本次没有产生任何提案")
    except Exception as exc:  # noqa: BLE001 - the author keeps typing either way
        warnings.append("模型调用失败：%s" % exc)
    if payload is None:
        reply, proposal = propose(None, world, existing)
        proposal["warnings"] = warnings + ["模型没有返回可用内容"]
        return reply, proposal
    reply, proposal = propose(payload, world, existing)
    proposal["warnings"] = warnings + list(proposal.get("warnings") or [])
    return reply, proposal


def commit_proposal(
    proposal: Dict[str, Any], world, scenario_store, game_store, scenario_id: str
) -> Dict[str, Any]:
    """Write a reviewed proposal. World document and knowledge only.

    Returns a result dict; a failure to save the world leaves knowledge alone,
    and the caller decides what to tell the author.
    """
    saved_world = False
    knowledge_ids: List[str] = []
    warnings: List[str] = []
    draft = proposal.get("world")
    if draft is not None:
        problems = draft.validate_references()
        if problems:
            warnings.append("世界引用校验失败：" + "；".join(problems[:8]))
        else:
            world_service.save_world(game_store, draft)
            saved_world = True
    for entry in proposal.get("knowledge") or []:
        try:
            stored = scenario_store.add_knowledge(scenario_id, entry)
            knowledge_ids.append(stored.id)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not lose the rest
            warnings.append("知识「%s」写入失败：%s" % (getattr(entry, "title", ""), exc))
    return {
        "world_saved": saved_world,
        "world_id": draft.world_id if (saved_world and draft is not None) else "",
        "knowledge_ids": knowledge_ids,
        "warnings": warnings,
    }
