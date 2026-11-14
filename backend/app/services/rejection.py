"""把一条没听懂的输入，变成一句玩家能接着往下走的话。

文本链路以前在解析失败时返回 status="skipped" 的空叙事，玩家只看到
「（行动被规则拒绝）」，既不知道错在哪，也不知道现场有什么可以动。
规则引擎本身没错：它只接受世界表里真实存在的名字。所以这里做的事是
把同样的约束讲清楚——列出此刻真的能用的出口、人物、物品与门。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.game import GameState, WorldDefinition

_MAX_HINTS = 3

#: 解析器的 note 用英文写（日志与 API 字段都是英文）。给玩家的那句话里
#: 不能出现 "could not resolve a target from '四周'" 这种内部措辞。
_NOTE_ZH = {
    "missing_target": "这里没有叫这个名字的东西",
    "ambiguous": "有好几个都能对上这个名字，说得具体一点",
    "no_target": "没找到要针对的目标",
    "unparsed_action": "",
}


def _note_line(code: str, detail: str, chinese: bool) -> str:
    """一句可读的原因；中文世界里绝不回显英文 note。"""
    if not code and not detail:
        return ""
    if chinese:
        return _NOTE_ZH.get(code, "没对上一个能操作的东西" if detail else "")
    # 英文世界也只讲人话：机器码（unparsed_action / missing_target）属于日志。
    if detail:
        return detail
    return "no such thing here" if code else ""


def _unique(names: Sequence[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for name in names:
        cleaned = (name or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _here(world: WorldDefinition, state: GameState):
    return next((loc for loc in world.locations if loc.id == state.location_id), None)


def available_actions(
    world: WorldDefinition, state: GameState, *, chinese: bool
) -> List[str]:
    """这一轮真的做得到的动作，最多三条，先出门再动手。"""
    here = _here(world, state)
    if here is None:
        return []
    locations = {loc.id: loc for loc in world.locations}
    exits = _unique([
        (locations.get(ex.target_id).name if locations.get(ex.target_id) else "") or ex.label
        for ex in here.exits
    ])
    people = _unique([
        entity.name for entity in world.entities
        if entity.id in (here.npc_ids or [])
    ])
    items = _unique([
        item.name for item in world.items
        if state.item_locations.get(item.id) == state.location_id
    ])
    out: List[str] = []
    for name in exits[:_MAX_HINTS]:
        out.append(f"前往 {name}" if chinese else f"go to {name}")
    if len(out) < _MAX_HINTS and people:
        out.append(f"对 {people[0]} 说话" if chinese else f"talk to {people[0]}")
    if len(out) < _MAX_HINTS and items:
        out.append(f"拿起 {items[0]}" if chinese else f"take {items[0]}")
    return out[:_MAX_HINTS]


def rejection_message(
    world: WorldDefinition,
    state: GameState,
    *,
    text: str = "",
    note: str = "",
    code: str = "",
) -> str:
    """一句可读的说明：没听懂 + 现场能做的一两件事。"""
    chinese = (getattr(world, "language", "") or "").lower().startswith("zh")
    stripped = (text or "").strip()
    echo = stripped.splitlines()[0][:40] if stripped else ""
    options = available_actions(world, state, chinese=chinese)
    explained = _note_line(code, note, chinese)
    if chinese:
        head = f"「{echo}」这句话没能对上眼前的任何东西。" if echo else "这句话没能对上眼前的任何东西。"
        body = "可以试试：" + "、".join(options) + "。" if options else "可以试试「看」，或者直接说你想怎么走。"
        tail = "（名字要和世界里现有的一致：地点、人物、物品都用它们现在的称呼。）"
        if explained:
            tail = f"（{explained}）" + tail
        return f"{head}{body}{tail}"
    head = f"I could not match “{echo}” to anything here." if echo else "I could not match that to anything here."
    body = "Try: " + ", ".join(options) + "." if options else "Try “look”, or say plainly where you want to go."
    tail = " (Names must match what already exists in this world.)"
    if explained:
        tail = f" ({explained})" + tail
    return f"{head} {body}{tail}"
