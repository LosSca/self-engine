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
    doors: Sequence[str] = (),
    door_ids: Sequence[str] = (),
    keys: Sequence[str] = (),
    key_ids: Sequence[str] = (),
    here_items: Sequence[str] = (),
    here_item_ids: Sequence[str] = (),
) -> list[Suggestion]:
    """Three next-step options built only from names the state already knows.

    Ordered by what actually moves the turn forward *right now*: the key that
    opens a locked door on the only way out, a shut door you can open, an item
    lying within reach, somewhere concrete to walk, then a person to address,
    then a small act that always works. Whatever the world lacks is skipped;
    generic lines fill the gap so the player always gets three buttons.

    ``exits`` must already be limited to ways the rules would let the player
    walk; a locked exit is passed through ``doors`` instead, and its key
    through ``keys`` when it is in this room or already carried.
    """
    zh = not language or language.lower().startswith("zh")
    # A shut door and a blocking key lead: "walk to X" behind a locked door is
    # the one option the rules are guaranteed to refuse.
    shut_doors = [n for n in (_text(x) for x in doors) if n][:1]
    shut_door_ids = [_text(x) for x in door_ids][:1]
    open_keys = [n for n in (_text(x) for x in keys) if n][:1]
    open_key_ids = [_text(x) for x in key_ids][:1]
    places = [n for n in (_text(x) for x in exits) if n][:3]
    reachable = [n for n in (_text(x) for x in here_items) if n][:2]
    reachable_ids = [_text(x) for x in here_item_ids][:2]
    people = [n for n in (_text(x) for x in characters) if n][:3]
    things = [n for n in (_text(x) for x in items) if n][:2]
    # The first character's entity id lets a click address them directly
    # instead of relying on the name being parsed back out of the prose.
    person_id = _text(character_ids[0]) if len(character_ids) else ""

    def pointed(entity_id: str) -> dict[str, str]:
        # The id lets a click address the real object instead of hoping the
        # client parses the name back out of the prose.
        return {"target_id": entity_id} if entity_id else {}

    candidates: list[Suggestion] = []

    # The key first: it is the step that makes the locked exit a real choice.
    if open_keys:
        key = open_keys[0]
        hint = pointed(open_key_ids[0] if open_key_ids else "")
        if zh:
            candidates.append({"action": f"拿起{key}", "speech": "", "target": key, **hint})
        else:
            candidates.append({"action": f"Pick up the {key}", "speech": "", "target": key, **hint})

    if shut_doors:
        door = shut_doors[0]
        hint = pointed(shut_door_ids[0] if shut_door_ids else "")
        if zh:
            candidates.append({"action": f"打开{door}", "speech": "", "target": door, **hint})
        else:
            candidates.append({"action": f"Open the {door}", "speech": "", "target": door, **hint})

    if reachable:
        item = reachable[0]
        hint = pointed(reachable_ids[0] if reachable_ids else "")
        if zh:
            candidates.append({"action": f"拿起{item}", "speech": "", "target": item, **hint})
        else:
            candidates.append({"action": f"Pick up the {item}", "speech": "", "target": item, **hint})

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


def _names(items: Any, attr: str = "name") -> list[str]:
    return [n for n in (_text(getattr(x, attr, None)) for x in (items or [])) if n]


def _room_facts(
    world: Any,
    state: Mapping[str, Any] | None,
    *,
    location_id: str = "",
) -> dict[str, list[str]]:
    """What is true of the player's room *right now*, in readable names.

    The one-turn rule is the whole point: a door that just opened, a key that
    just got picked up and a person who just walked out all have to show up in
    the next three buttons, so every list here is recomputed from the state
    that is about to be shown to the player.

    * exits      -- neighbour rooms the rules would let them walk to,
    * doors      -- doors in this room that are still shut,
    * door_ids   -- the ids of those doors,
    * keys       -- keys for those doors that are here or already carried,
    * key_ids    -- the ids of those keys,
    * here_items -- items lying within reach,
    * here_item_ids -- their ids,
    * characters -- people actually in this room, not the whole cast.
    """
    facts: dict[str, list[str]] = {
        "exits": [], "doors": [], "door_ids": [], "keys": [], "key_ids": [],
        "here_items": [], "here_item_ids": [], "characters": [], "_here_name": [],
    }
    if world is None:
        return facts
    state = state if isinstance(state, Mapping) else {}
    locations = {
        getattr(loc, "id", ""): loc for loc in (getattr(world, "locations", []) or [])
    }
    wanted = _text(location_id) or _text(state.get("location_id"))
    here = locations.get(wanted)
    if here is None and wanted:
        # The player is standing somewhere the world document does not define
        # (a rebuilt world, a renamed room). Guessing the start room here would
        # describe a different place entirely.
        return facts
    if here is None:
        # A campaign that has not acted yet has no location: the world's own
        # start room is the only honest answer.
        start = _text(getattr(world, "start_location_id", ""))
        here = locations.get(start) or next(iter(locations.values()), None)
    if here is None:
        return facts

    here_id = _text(getattr(here, "id", ""))
    inventory = state.get("inventory") if isinstance(state.get("inventory"), Mapping) else {}
    door_state = state.get("doors") if isinstance(state.get("doors"), Mapping) else {}
    item_at = state.get("item_locations") if isinstance(state.get("item_locations"), Mapping) else {}
    doors = {_text(getattr(d, "id", "")): d for d in (getattr(world, "doors", []) or [])}
    items = {_text(getattr(i, "id", "")): i for i in (getattr(world, "items", []) or [])}

    def door_is_open(door: Any) -> bool:
        door_id = _text(getattr(door, "id", ""))
        live = door_state.get(door_id)
        if isinstance(live, bool):
            return live
        return bool(getattr(door, "opened", False))

    for exit_def in getattr(here, "exits", []) or []:
        target = locations.get(_text(getattr(exit_def, "target_id", "")))
        label = _text(getattr(exit_def, "label", ""))
        name = label or (_text(getattr(target, "name", "")) if target is not None else "")
        door = doors.get(_text(getattr(exit_def, "door_id", "")) or "")
        if door is not None and not door_is_open(door):
            # Walking this way is refused until the door opens, so it is not a
            # destination yet: it is a door to open.
            door_name = _text(getattr(door, "name", "")) or name
            if door_name and door_name not in facts["doors"]:
                facts["doors"].append(door_name)
                facts["door_ids"].append(_text(getattr(door, "id", "")))
            key_id = _text(getattr(door, "key_item_id", ""))
            key = items.get(key_id) if key_id else None
            try:
                reachable = item_at.get(key_id) == here_id
            except TypeError:
                reachable = False
            # Only a key still lying here is something to pick up; a key
            # already carried needs no further step, so it is not offered.
            if (
                key is not None
                and reachable
                and not inventory.get(key_id)
                and key_id not in facts["key_ids"]
            ):
                facts["keys"].append(_text(getattr(key, "name", "")))
                facts["key_ids"].append(key_id)
            continue
        need = _text(getattr(exit_def, "requires_item_id", ""))
        if need and not inventory.get(need):
            continue
        if name:
            facts["exits"].append(name)

    room_items = set(getattr(here, "item_ids", []) or [])
    for item_id, item in items.items():
        try:
            at = item_at.get(item_id)
        except TypeError:
            at = None
        if item_id in inventory and inventory.get(item_id):
            # Carried: it is in the player's hands. The state is the authority
            # here - a stale item_locations row must not put it back on the
            # floor and offer "pick it up" again.
            continue
        # The state is authoritative; the room's authored list is what makes an
        # item visible before the first commit placed it.
        if at == here_id or (at is None and item_id in room_items):
            facts["here_items"].append(_text(getattr(item, "name", "")))
            facts["here_item_ids"].append(item_id)

    live = state.get("entities") if isinstance(state.get("entities"), Mapping) else {}
    for entity in getattr(world, "entities", []) or []:
        if getattr(entity, "kind", "npc") == "object":
            continue
        entity_id = _text(getattr(entity, "id", ""))
        record = live.get(entity_id)
        if isinstance(record, Mapping):
            # A plain-dict record (e.g. a model_dump) keeps its place under the
            # same key the model uses.
            place = _text(record.get("location_id", ""))
        elif isinstance(record, str):
            # A dump that is already flattened to {"id": "place"} is still a
            # live position; only a missing record means "where the author put
            # them".
            place = _text(record)
        else:
            place = _text(getattr(record, "location_id", "")) if record is not None else ""
        if not place:
            place = _text(getattr(entity, "location_id", ""))
        # Where they are *now* wins. The room's npc_ids list is authored and is
        # never rewritten when someone walks off, so trusting it would keep
        # offering "talk to X" to an empty room.
        if place == here_id:
            facts["characters"].append(_text(getattr(entity, "name", "")))

    facts["_here_name"] = [_text(getattr(here, "name", ""))] if _text(getattr(here, "name", "")) else []
    return facts


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

    The room is resolved from the state *after* the turn, so a door that just
    opened, a key that was just taken and the people still standing here are
    what the three buttons describe.
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
    try:
        if world is None and callable(world_getter):
            world = world_getter()
    except Exception:  # noqa: BLE001 - suggestions are advisory
        world = None
    facts = _room_facts(world, state, location_id=location_id)

    # Only people who are in this room. A caller's cast list is a fallback for
    # worlds whose entities the builder cannot read at all - naming someone who
    # is elsewhere is exactly what made every talk suggestion a rejection.
    people = [(n, "") for n in facts["characters"]]
    if not people and not (getattr(world, "entities", None) or []):
        people = list(characters)

    items_by_id = {}
    try:
        items_by_id = {i.id: i for i in (getattr(world, "items", []) or [])}
    except AttributeError:
        items_by_id = {}
    item_names = [
        _text(getattr(items_by_id[i], "name", "")) for i in held_ids if i in items_by_id
    ]

    return build_suggestions(
        location=(facts["_here_name"] or [""])[0],
        exits=facts["exits"],
        doors=facts["doors"],
        door_ids=facts["door_ids"],
        keys=facts["keys"],
        key_ids=facts["key_ids"],
        here_items=facts["here_items"],
        here_item_ids=facts["here_item_ids"],
        characters=[name for name, _ in people],
        character_ids=[entity_id for _, entity_id in people],
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
    doors: Mapping[str, Any] | None = None,
    item_locations: Mapping[str, str] | None = None,
    entities: Mapping[str, Any] | None = None,
) -> list[Suggestion]:
    """Three options for the state a campaign is in *right now*.

    The turn-scoped builder above needs a finished ActionResult. The opening
    screen has no turn yet, and the client wants buttons before the first
    action, so this takes the raw state fields instead (location id, held item
    ids, cast) and resolves names from the world document.

    ``doors`` / ``item_locations`` / ``entities`` are the live overrides from the
    committed state: without them the builder would describe the world as it
    was authored, e.g. still calling a door "locked" three turns after it was
    opened and offering a walk the rules now allow.
    """
    view: dict[str, Any] = {
        "location_id": _text(location_id),
        "inventory": {_text(k): 1 for k in inventory if _text(k)},
    }
    if isinstance(doors, Mapping):
        view["doors"] = dict(doors)
    if isinstance(item_locations, Mapping):
        view["item_locations"] = dict(item_locations)
    if isinstance(entities, Mapping):
        view["entities"] = {}
        for key, record in entities.items():
            place = getattr(record, "location_id", None)
            if place is None and isinstance(record, Mapping):
                place = record.get("location_id")
            view["entities"][_text(key)] = {"location_id": _text(place)}
    try:
        facts = _room_facts(world, view, location_id=view["location_id"])
    except Exception:  # noqa: BLE001 - hints are advisory
        facts = _room_facts(None, view)
    people = [(n, "") for n in facts["characters"]] or list(characters)
    item_names: list[str] = []
    try:
        items_by_id = {item.id: item for item in getattr(world, "items", []) or []}
        item_names = [items_by_id[key].name for key in inventory if key in items_by_id]
    except (AttributeError, KeyError, TypeError):
        item_names = []
    return build_suggestions(
        location=(facts["_here_name"] or [""])[0],
        exits=facts["exits"],
        doors=facts["doors"],
        door_ids=facts["door_ids"],
        keys=facts["keys"],
        key_ids=facts["key_ids"],
        here_items=facts["here_items"],
        here_item_ids=facts["here_item_ids"],
        characters=[name for name, _ in people],
        character_ids=[entity_id for _, entity_id in people],
        items=item_names,
        language=language,
    )


def fingerprint(items: Sequence[Suggestion]) -> str:
    """Stable id for a suggestion set (lets the client dedupe/track picks)."""
    joined = "|".join(f"{s.get('action', '')}~{s.get('speech', '')}" for s in items)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]