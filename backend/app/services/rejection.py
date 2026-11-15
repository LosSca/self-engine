"""把一条没听懂的输入，变成一句玩家能接着往下走的话。

文本链路以前在解析失败时返回 status="skipped" 的空叙事，玩家只看到
「（行动被规则拒绝）」，既不知道错在哪，也不知道现场有什么可以动。
规则引擎本身没错：它只接受世界表里真实存在的名字。所以这里做的事是
把同样的约束讲清楚——列出此刻真的能用的出口、人物、物品与门。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, List, Sequence

from app.utils.language import detect_language

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.game import GameState, WorldDefinition

_MAX_HINTS = 3

#: 解析器的 note 用英文写（日志与 API 字段都是英文）。给玩家的那句话里
#: 不能出现 "could not resolve a target from '四周'" 这种内部措辞。
_NOTE_ZH = {
    "missing_target": "这里没有叫这个名字的东西",
    "nobody_here": "这里一个人也没有",
    "ambiguous": "有好几个都能对上这个名字，说得具体一点",
    "no_target": "没找到要针对的目标",
    "unparsed_action": "",
}


def _is_envelope_only(text: str) -> bool:
    """True when the player sent nothing but a talk-type tag ("[CONTINUE]").

    Such a tag is an instruction to the narrator, not an utterance the parser
    can read, so echoing it back ("「[CONTINUE]」这句话…") only repeats the
    machine syntax at the player. The message drops the echo instead.
    """
    stripped = (text or "").strip()
    return bool(re.fullmatch(r"\[[A-Z]+\]", stripped, re.IGNORECASE))


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


def _who_is_where(
    world: WorldDefinition, state: GameState, text: str, *, chinese: bool
) -> str:
    """Answer a sentence that names somebody who is simply elsewhere.

    "向守卫开口" with no guard in the room is not a parse failure the player
    can learn from - it is a question about where that person went. Saying
    "there is nothing by that name here, try: visit the warehouse" answers a
    different question, which is what made talking feel unresponsive.
    """
    lowered = (text or "").lower()
    if not lowered:
        return ""
    here = _here(world, state)
    here_id = here.id if here is not None else state.location_id
    places = {loc.id: loc for loc in world.locations}
    for entity in world.entities:
        if entity.kind == "object":
            continue
        name = (entity.name or "").strip()
        if not name or name.lower() not in lowered:
            continue
        # Where they are *now*, not where the author put them: an NPC who
        # walked off is not still at their starting room, and telling the
        # player otherwise sends them to the wrong place.
        current = _entity_place(state, entity)
        if current == here_id:
            return ""  # they are here; the action was about something else
        where = places.get(current)
        place = where.name if where is not None else ""
        if chinese:
            return f"{name}不在这里，在{place}。" if place else f"{name}不在这里。"
        return (
            f"{name} is not here - they are at {place}."
            if place
            else f"{name} is not here."
        )
    return ""


def _entity_place(state: GameState, entity) -> str:
    """Where an entity is now: the run-time record wins over the authored one."""
    record = getattr(state, "entities", {}).get(entity.id)
    live = getattr(record, "location_id", "") if record is not None else ""
    return live or entity.location_id


def _people_nearby(
    world: WorldDefinition, state: GameState, *, chinese: bool
) -> str:
    """"Nobody by that name — the people you can reach are …"."""
    here = _here(world, state)
    here_id = here.id if here is not None else state.location_id
    present = [
        entity.name for entity in world.entities
        if entity.kind != "object" and _entity_place(state, entity) == here_id
    ]
    if not present:
        return (
            "这里一个人也没有。"
            if chinese
            else "There is nobody here to talk to."
        )
    names = "、".join(present[:3])
    if chinese:
        return f"这里没有叫这个名字的人。眼前有：{names}。"
    return f"There is nobody by that name here. Present: {', '.join(present[:3])}."


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


def _mentioned(text: str, name: str) -> bool:
    """Whether the player actually said this name (or a piece of it)."""
    needle = (name or "").strip().lower()
    haystack = (text or "").lower()
    if not needle or not haystack:
        return False
    if needle in haystack:
        return True
    # "仓库门" for "仓库", "守林人" for "守林": a name the player half-said
    # still means that thing, and it is the best signal of what they want.
    for size in (3, 2):
        if len(needle) >= size and any(
            needle[i: i + size] in haystack for i in range(len(needle) - size + 1)
        ):
            return True
    return False


def available_actions(
    world: WorldDefinition, state: GameState, *, chinese: bool, text: str = ""
) -> List[str]:
    """这一轮真的做得到的动作，最多三条，先回应玩家自己提到的东西。

    The list used to be identical on every turn because it only described the
    room. A player who says "走向仓库" and gets told to visit the warehouse, the
    forest and the key every time reads it as "it did not hear me". What was
    named comes first, and only then the generic way forward.
    """
    here = _here(world, state)
    if here is None:
        return []
    locations = {loc.id: loc for loc in world.locations}
    exits = _unique([
        (locations.get(ex.target_id).name if locations.get(ex.target_id) else "") or ex.label
        for ex in here.exits
    ])
    # An NPC can be placed in a room two ways: the room lists them, or the
    # entity says where it lives. Only honouring the first meant a world that
    # places people by `location_id` got no "talk to X" advice at all.
    room_ids = set(here.npc_ids or [])
    people = _unique([
        entity.name for entity in world.entities
        if entity.id in room_ids
        or (entity.kind != "object" and _entity_place(state, entity) == state.location_id)
    ])
    # An item counts as "here" either because the state places it here or
    # because the room lists it: the two disagree in authored worlds, and a
    # key the player can plainly see must not be missing from the advice.
    room_items = set(here.item_ids or [])
    items = _unique([
        item.name for item in world.items
        if state.item_locations.get(item.id) == state.location_id
        or item.id in room_items
    ])
    out: List[str] = []
    # What the player just named leads the answer. Being told about the three
    # things you did not ask about, every single turn, is what made the game
    # feel deaf.
    named_exits = [name for name in exits if _mentioned(text, name)]
    named_people = [name for name in people if _mentioned(text, name)]
    named_items = [name for name in items if _mentioned(text, name)]
    for name in named_exits[:_MAX_HINTS]:
        out.append(f"前往 {name}" if chinese else f"go to {name}")
    if len(out) < _MAX_HINTS:
        for name in named_people:
            out.append(f"对 {name} 说话" if chinese else f"talk to {name}")
    if len(out) < _MAX_HINTS:
        for name in named_items:
            out.append(f"拿起 {name}" if chinese else f"take {name}")
    if out:
        return out[:_MAX_HINTS]
    # Nothing they named is here: fall back to the room, which is still the
    # honest answer to "there is nothing by that name".
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
    # An unset language is the common case, not an English world: infer it from
    # the world's own prose so a Chinese world never answers in English.
    chinese = detect_language(world) == "zh"
    stripped = (text or "").strip()
    echo = "" if _is_envelope_only(stripped) else (stripped.splitlines()[0][:40] if stripped else "")
    options = available_actions(world, state, chinese=chinese, text=stripped)
    explained = _note_line(code, note, chinese)
    # A sentence that named a real person who is elsewhere gets answered about
    # that person, before any generic "try visiting the warehouse".
    where = _who_is_where(world, state, stripped, chinese=chinese)
    if where:
        return where
    # Nobody by that name anywhere: say who *is* reachable instead of listing
    # the room's furniture. "There is nothing by that name, try: go to the
    # warehouse" reads as an answer to a different question.
    if code == "missing_target" and not where:
        others = _people_nearby(world, state, chinese=chinese)
        if others:
            return others
    if chinese:
        body = _try_line(options, chinese)
        # The reason is the only part that teaches the player something they
        # could not see for themselves; the rest of the old message was a style
        # guide ("names must match the world") dressed up as an error report.
        # The player's own sentence is *not* a reason: repeating it back adds
        # nothing and reads like a parser talking to itself.
        return f"{explained}。{body}" if explained else body
    body = _try_line(options, chinese)
    return f"{explained} {body}" if explained else body


def _try_line(options: Sequence[str], chinese: bool) -> str:
    """The suggestions line, after the reason has been stated.

    "可以试试…" is itself a verb, so it has to follow a full stop: gluing an
    explanation to it with a comma produced "这里没有叫这个名字的东西，可以
    试试…", which is not how the sentence reads in Chinese.
    """
    if chinese:
        if options:
            return "可以试试：" + "、".join(options) + "。"
        return "可以试试「看」，或者直接说你想怎么走。"
    if options:
        return "Try: " + ", ".join(options) + "."
    return "Try “look”, or say plainly where you want to go."
