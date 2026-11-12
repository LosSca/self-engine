"""Three concrete next steps for the player.

Why this exists: suggestions used to come only from the narrator JSON field
(engines/narrator_engine.py) and from an extra LLM call
(services/game_session.py:_generate_suggestions). Both die with the model: with
no API key the turn degrades to a template and the player is left with an empty
input box. The rule lane (action_service) never asked for suggestions at all,
so engine-bound campaigns got none.

This module answers "what are three sensible things to do right now" from
facts the server already has:

* where the player is and which exits exist (rule state),
* nearby characters (campaign cast, NPC minds, graph entities),
* one carried item to try.

The result is advisory: nothing is committed and no name is invented. The
narrator may still be asked for better options; this is the floor.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping, Sequence

# Every option uses the shape the narrator lane already emits, so the SSE
# [SUGGESTIONS] control tag and the client stay unchanged.
Suggestion = dict[str, str]

_MOVE_VERBS_ZH = ("走向", "进入", "前往")
_MOVE_VERBS_EN = ("head to", "step into", "walk toward")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _attributes(node: Any) -> Mapping[str, Any]:
    if isinstance(node, Mapping):
        attrs = node.get("attributes")
        return attrs if isinstance(attrs, Mapping) else {}
    attrs = getattr(node, "attributes", None)
    return attrs if isinstance(attrs, Mapping) else {}


def _name(value: Any) -> str:
    if isinstance(value, Mapping):
        return _text(value.get("name")) or _text(value.get("title"))
    return _text(getattr(value, "name", None)) or _text(getattr(value, "title", None))


def _norm(item: Suggestion) -> Suggestion:
    """Normalise one option.

    ``target`` is the display name; ``target_id`` is the entity id when the
    caller knows it, so the client can address the action at a real character
    instead of hoping the name is parsed back out of the prose.
    """
    out: Suggestion = {
        "action": _text(item.get("action")),
        "speech": _text(item.get("speech")),
        "target": _text(item.get("target")),
    }
    target_id = _text(item.get("target_id"))
    if target_id:
        out["target_id"] = target_id
    return out


def _dedupe(items: Iterable[Suggestion], limit: int = 3) -> list[Suggestion]:
    """Keep the first option per (action, speech) pair, drop empty ones."""
    out: list[Suggestion] = []
    seen: set[tuple[str, str]] = set()
    for raw in items:
        item = _norm(raw)
        if not (item["action"] or item["speech"]):
            continue
        key = (item["action"].casefold(), item["speech"].casefold())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def exits_from_state(state: Any) -> list[str]:
    """Exit names from a rule-engine GameState (or a dict shaped like one).

    The engine stores exits as ``{here: {direction_or_room: room_name}}``;
    older payloads used a flat list of names. Both are accepted, and a
    missing or malformed block simply yields no exits.
    """
    exits = state.get("exits") if isinstance(state, Mapping) else getattr(state, "exits", None)
    names: list[str] = []
    if isinstance(exits, Mapping):
        for bucket in exits.values():
            if isinstance(bucket, Mapping):
                names.extend(_text(v) for v in bucket.values())
            elif isinstance(bucket, (list, tuple)):
                names.extend(_text(v) for v in bucket)
            else:
                names.append(_text(bucket))
    elif isinstance(exits, (list, tuple)):
        names.extend(_text(v) for v in exits)
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def build_suggestions(
    *,
    location: str = "",
    exits: Sequence[str] = (),
    characters: Sequence[str] = (),
    character_ids: Sequence[str] = (),
    items: Sequence[str] = (),
    language: str = "zh",
) -> list[Suggestion]:
    """Three next-step options built only from names the state already knows.

    Ordered by how much context each needs: move somewhere concrete, then
    engage a person, then a small act that always works. Whatever the world
    lacks is skipped; generic lines fill the gap so the player always gets
    three buttons.
    """
    zh = not language or language.lower().startswith("zh")
    places = [n for n in (_text(x) for x in exits) if n][:3]
    people = [n for n in (_text(x) for x in characters) if n][:3]
    things = [n for n in (_text(x) for x in items) if n][:2]
    # The first character's entity id lets a click address them directly
    # instead of relying on the name being parsed back out of the prose.
    person_id = _text(character_ids[0]) if len(character_ids) else ""

    candidates: list[Suggestion] = []

    if places:
        verb = _MOVE_VERBS_ZH[0] if zh else _MOVE_VERBS_EN[0]
        action = f"{verb}{places[0]}" if zh else f"{verb} {places[0]}"
        candidates.append({"action": action, "speech": "", "target": places[0]})

    if people:
        person = people[0]
        if zh:
            candidates.append({"action": "", "speech": f"向{person}开口，问这里发生了什么", "target": person, "target_id": person_id})
            candidates.append({"action": f"打量{person}，留意对方的反应", "speech": "", "target": person, "target_id": person_id})
        else:
            candidates.append({"action": "", "speech": f"Ask {person} what happened here", "target": person, "target_id": person_id})
            candidates.append({"action": f"Study {person} and watch how they react", "speech": "", "target": person, "target_id": person_id})

    if things:
        item = things[0]
        if zh:
            candidates.append({"action": f"取出{item}，看看它现在的状态", "speech": "", "target": item})
        else:
            candidates.append({"action": f"Take out the {item} and check it", "speech": "", "target": item})

    if places[1:]:
        verb = _MOVE_VERBS_ZH[1] if zh else _MOVE_VERBS_EN[1]
        for place in places[1:]:
            action = f"{verb}{place}" if zh else f"{verb} {place}"
            candidates.append({"action": action, "speech": "", "target": place})

    # Always-available fallbacks. An earlier version offered "停下来，先理清
    # 眼前的处境" and "耐心等一等" — the rule lane resolves neither, so every
    # click came straight back as a rejection, which reads as dead air. The
    # parser always accepts a bare "look" (and 查看/观察/打量…), so the floor
    # is now that one act, phrased against the real room name when there is one.
    if zh:
        candidates.append({"action": "看", "speech": ""})
        if location:
            candidates.append({"action": f"查看{location}", "speech": ""})
        candidates.append({"action": "观察四周", "speech": ""})
    else:
        candidates.append({"action": "look", "speech": ""})
        if location:
            candidates.append({"action": f"Look around {location}", "speech": ""})
        candidates.append({"action": "Take a careful look around", "speech": ""})

    picks = _dedupe(candidates)
    # A sparse world (no cast, no exits) can still leave fewer than three; the
    # UI promises three buttons, so fill the tail with neutral acts.
    # Same rule for the tail: only acts the engine accepts without a target.
    filler = (
        [
            {"action": "看看脚下", "speech": ""},
            {"action": "环顾四周", "speech": ""},
            {"action": "观察近处", "speech": ""},
        ]
        if zh
        else [
            {"action": "Glance around", "speech": ""},
            {"action": "Look down at your feet", "speech": ""},
            {"action": "Study everything nearby", "speech": ""},
        ]
    )
    # Appended directly instead of through _dedupe: that helper stops at
    # three, so re-running it over a full list would never extend it.
    for extra in filler:
        if len(picks) >= 3:
            break
        key = (extra["action"].casefold(), extra["speech"].casefold())
        if any((p["action"].casefold(), p["speech"].casefold()) == key for p in picks):
            continue
        picks = [*picks, _norm(extra)]
    return picks


def pick_target(text: str, names: Sequence[str]) -> tuple[str, str]:
    """Find the character an utterance is aimed at.

    Returns ``(name, mentioned_text)``. Matching is token-aware: a name
    matches when it appears whole (``@Satoru Gojo``) or, for CJK names where
    spaces are not word separators, as a substring of at least two chars.
    Longer names win so "Ash" never shadows "Ashen Priest".
    """
    body = _text(text)
    if not body:
        return "", ""
    # An explicit @handle is the player pointing at someone; it wins over
    # any incidental substring elsewhere in the sentence.
    lowered = body.casefold()
    for raw in sorted((_text(n) for n in names if _text(n)), key=len, reverse=True):
        if f"@{raw}".casefold() in lowered:
            return raw, f"@{raw}"
    for raw in sorted((_text(n) for n in names if _text(n)), key=len, reverse=True):
        if len(raw) >= 2 and raw.casefold() in lowered:
            return raw, raw
    return "", ""


def target_directive(name: str, language: str = "zh") -> str:
    """One system-prompt line binding the turn prose to a named character."""
    target = _text(name)
    if not target:
        return ""
    if language and not language.lower().startswith("zh"):
        return (
            f"The player is speaking to or acting on {target}. Show {target}'s"
            f" reaction in their own voice; do not let {target} act out of character."
        )
    return (
        f"玩家这一回合在对「{target}」说话或行动。叙事必须写出「{target}」本人的反应与回答，"
        "保持该角色的性格与已知设定，不要替玩家决定结果。"
    )


def suggestions_for_result(
    result: Any,
    *,
    characters: Iterable[tuple[str, str]] = (),
    world_getter: Any = None,
    language: str = "zh",
) -> list[Suggestion]:
    """Three advisory next steps for one committed turn.

    Turn state only holds location ids and an item-id -> count map, so the
    readable names and exits come from the world document the engine resolved
    for the turn (result["world"]) with world_getter as a fallback. Everything
    is optional: an unreadable world narrows the list to the generic lines
    rather than failing the turn.
    """
    payload = result if isinstance(result, dict) else {}
    state = payload.get("state") or {}
    if not isinstance(state, dict):
        dump = getattr(state, "model_dump", None)
        state = dump() if callable(dump) else {}
    world = payload.get("world")

    location_id = str(state.get("location_id") or "")
    inventory = state.get("inventory")
    held_ids = list(inventory) if isinstance(inventory, dict) else []

    location_name = ""
    place_names: list[str] = []
    item_names: list[str] = []
    try:
        if world is None and callable(world_getter):
            world = world_getter()
        locations = {loc.id: loc for loc in getattr(world, "locations", []) or []}
        if location_id in locations:
            here = locations[location_id]
            location_name = here.name
            for exit_def in getattr(here, "exits", []) or []:
                target = locations.get(exit_def.target_id)
                label = (getattr(exit_def, "label", "") or "").strip()
                if label:
                    place_names.append(label)
                elif target is not None:
                    place_names.append(target.name)
        items_by_id = {item.id: item for item in getattr(world, "items", []) or []}
        item_names = [items_by_id[i].name for i in held_ids if i in items_by_id]
    except Exception:  # noqa: BLE001 - suggestions are advisory
        location_name, place_names, item_names = "", [], []

    present = list(characters)
    return build_suggestions(
        location=location_name,
        exits=place_names,
        characters=[name for name, _ in present],
        character_ids=[entity_id for _, entity_id in present],
        items=item_names,
        language=language,
    )

def hints_from_state(
    *,
    location_id: str = "",
    inventory: Iterable[str] = (),
    characters: Iterable[tuple[str, str]] = (),
    world: Any = None,
    language: str = "zh",
) -> list[Suggestion]:
    """Three options for the state a campaign is in *right now*.

    The turn-scoped builder above needs a finished ActionResult. The opening
    screen has no turn yet, and the client wants buttons before the first
    action, so this takes the raw state fields instead (location id, held item
    ids, cast) and resolves names from the world document.
    """
    areas: list[str] = []
    location_name = ""
    item_names: list[str] = []
    try:
        locations = {loc.id: loc for loc in getattr(world, "locations", []) or []}
        # A campaign that has not taken a turn yet has no location id; the
        # world it will start in is the only meaningful answer.
        here = locations.get(location_id) or next(
            iter([loc for loc in locations.values()]), None
        )
        if here is not None:
            location_name = here.name
            for exit_def in getattr(here, "exits", []) or []:
                label = (getattr(exit_def, "label", "") or "").strip()
                target = locations.get(exit_def.target_id)
                if label:
                    areas.append(label)
                elif target is not None:
                    areas.append(target.name)
        items_by_id = {item.id: item for item in getattr(world, "items", []) or []}
        item_names = [
            items_by_id[key].name for key in inventory if key in items_by_id
        ]
    except Exception:  # noqa: BLE001 - hints are advisory
        location_name, areas, item_names = "", [], []
    present = list(characters)
    return build_suggestions(
        location=location_name,
        exits=areas,
        characters=[name for name, _ in present],
        character_ids=[entity_id for _, entity_id in present],
        items=item_names,
        language=language,
    )


def fingerprint(items: Sequence[Suggestion]) -> str:
    """Stable id for a suggestion set (lets the client dedupe/track picks)."""
    joined = "|".join(f"{s.get('action', '')}~{s.get('speech', '')}" for s in items)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]
