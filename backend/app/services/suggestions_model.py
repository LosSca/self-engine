"""Options written by the model, checked against what the rules can execute.

The engine lane used to offer three buttons from the rules alone: correct but
predictable. Here the narrator model writes them instead, from a *guidance*
pack (see suggestions.room_guidance) that names the only verbs the rules know
and the only targets standing in the room. Every returned option is then
verified against that pack -- a made-up room, item or person is dropped rather
than shown as a button that the engine would refuse -- and the rule options
fill any gap, so a turn always ends with three usable choices.

A failed or slow call is never fatal: the caller falls back to the rule list.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping, Sequence

from app.engines.action_registry import all_actions, verb_table
from app.services.suggestions import Suggestion, _norm, _text, allowed_targets

logger = logging.getLogger(__name__)

_MAX_ACTION = 40
_MAX_SPEECH = 60
_LIMIT = 3
_FENCE = chr(96) * 3
_NEWLINE = chr(10)

# Which kinds of name each verb may point at. Anything not listed only needs
# *some* known name; verbs with target "none" need none at all.
_KINDS: dict[str, tuple[str, ...]] = {
    "move": ("exit", "door"),
    "open": ("door", "item"),
    "take": ("item",),
    "use": ("item",),
    "equip": ("item",),
    "buy": ("item",),
    "sell": ("item",),
    "dismantle": ("item",),
    "combine": ("item",),
    "attack": ("hostile", "character"),
    "talk": ("character", "hostile"),
    "investigate": ("item", "door", "exit", "character", "hostile"),
}

_VERBS: list[tuple[str, str, str]] | None = None


def _verb_rows() -> list[tuple[str, str, str]]:
    """(verb, action name, target need), longest verb first."""
    global _VERBS
    if _VERBS is None:
        specs = {spec.name: spec for spec in all_actions()}
        rows: list[tuple[str, str, str]] = []
        for name, verbs in verb_table():
            need = str(getattr(specs.get(name), "target", "optional") or "optional")
            rows.extend((verb, name, need) for verb in verbs)
        rows.sort(key=lambda row: -len(row[0]))
        _VERBS = rows
    return _VERBS


def _match(action: str) -> tuple[str, str, str] | None:
    """Resolve the verb an option starts with -> (verb, action name, need)."""
    for verb, name, need in _verb_rows():
        if action.startswith(verb):
            return verb, name, need
    return None


def _names_in(text: str, targets: Mapping[str, str]) -> list[str]:
    """Known names mentioned by the text, longest first."""
    hits = [name for name in targets if name and name in text]
    hits.sort(key=len, reverse=True)
    return hits


def _has_cjk(text: str) -> bool:
    """True when the text reads as prose rather than an action identifier."""
    return any("一" <= ch <= "鿿" for ch in text)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strip_fence(text: str) -> str:
    block = text.strip()
    if _FENCE in block:
        parts = block.split(_FENCE)
        inner = [part for part in parts[1::2] if part.strip()]
        if inner:
            block = inner[0].strip()
    return block


def _json_slice(block: str) -> str:
    """The outermost JSON payload in a reply (array preferred over object)."""
    for opener, closer in (("[", "]"), ("{", "}")):
        start = block.find(opener)
        end = block.rfind(closer)
        if start != -1 and end > start:
            return block[start : end + 1]
    return ""


def check_option(
    raw: Any,
    targets: Mapping[str, str],
    language: str = "zh",
    *,
    allow_speech: bool = True,
    allow_investigate: bool = True,
) -> Suggestion | None:
    """One model option -> a suggestion, or None when a rule would refuse it."""
    if not isinstance(raw, Mapping):
        return None
    action = _text(raw.get("action"))
    speech = _text(raw.get("speech"))
    target = _text(raw.get("target"))

    if speech:
        if not allow_speech:
            # The author switched conversation options off for this world.
            return None
        kind = targets.get(target)
        if kind not in ("character", "hostile"):
            return None
        speech = _clip(speech, _MAX_SPEECH)
        if len(speech) < 2:
            return None
        return {"action": "", "speech": speech, "target": target}

    if not action:
        return None
    hit = _match(action)
    if hit is None:
        return None
    verb, name, need = hit
    if name == "investigate" and not allow_investigate:
        return None

    mentioned = _names_in(action, targets)
    if need == "required" and not mentioned:
        return None
    wanted = _KINDS.get(name)
    if wanted and mentioned:
        kinds = [targets.get(item, "") for item in mentioned]
        if not any(kind in wanted for kind in kinds):
            return None
    if target and target not in targets and target not in action:
        return None
    zh = _text(language) != "en"
    if zh and not _has_cjk(verb):
        # "investigate 检查" is the action name plus a verb, not prose a
        # Chinese player would pick from a button.
        return None
    if zh and not _has_cjk(action):
        return None
    action = _clip(action, _MAX_ACTION)
    # The chip names who is addressed; for anything else the prose already
    # carries the name, so repeating it would only duplicate the label.
    return {"action": action, "speech": "", "target": ""}


def parse_options(
    text: str,
    targets: Mapping[str, str],
    language: str = "zh",
    *,
    allow_speech: bool = True,
    allow_investigate: bool = True,
) -> list[Suggestion]:
    """Pull option objects out of a model reply, keeping only valid ones."""
    if not text:
        return []
    payload = _json_slice(_strip_fence(text))
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except ValueError:
        return []
    if isinstance(data, Mapping):
        data = data.get("options") or data.get("suggestions") or []
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return []
    out: list[Suggestion] = []
    for raw in data:
        option = check_option(
            raw,
            targets,
            language,
            allow_speech=allow_speech,
            allow_investigate=allow_investigate,
        )
        if option:
            out.append(option)
    return out


def guidance_prompt(
    guidance: Mapping[str, Any],
    *,
    count: int = _LIMIT,
    extra_rules: str = "",
    allow_speech: bool = True,
    allow_investigate: bool = True,
    use_story: bool = True,
) -> str:
    """The hard constraints shown to the model, room by room.

    Everything here is user-editable from the settings panel: how many options
    to ask for, whether talking or investigating may appear, and any extra rule
    the author writes. The switches only *narrow* the pack, never widen it.
    """
    count = max(2, min(int(count or _LIMIT), 5))

    def line(label: str, values: Any, fallback: str) -> str:
        items = [str(value).strip() for value in (values or []) if str(value).strip()]
        return label + "：" + ("、".join(items) if items else fallback)

    verbs = "；".join(name + "=" + "/".join(list(verbs)[:5]) for name, verbs in verb_table())
    rows = []
    recent = _text(guidance.get("recent"))
    if recent:
        rows.append("上一步刚发生：" + recent)
    story = guidance.get("story") if use_story else None
    if isinstance(story, Mapping) and story.get("next"):
        chapter = _text(story.get("chapter"))
        goal = [str(item).strip() for item in (story.get("goal") or []) if str(item).strip()]
        rows.append(
            "故事进度："
            + (f"当前章节《{chapter}》；" if chapter else "")
            + f"下一章《{_text(story.get('next'))}》"
            + (("需要到达：" + "、".join(goal)) if goal else "")
            + f"。{count} 条选项中必须有且只有一条直接推进到下一章"
            + (f"（写成「前往{goal[0]}」）" if goal else "")
            + "。"
        )
    rows += [
        line("当前地点", [guidance.get("location")], "未知"),
        line("可前往的出口", guidance.get("exits"), "无"),
        line("还没打开的门", guidance.get("doors"), "无"),
        line("可用的钥匙", guidance.get("keys"), "无"),
        line("身边可拾取的物品", guidance.get("here_items"), "无"),
        line("在场人物", guidance.get("characters"), "无"),
        line("敌对生物", guidance.get("hostiles"), "无"),
        line("随身携带", guidance.get("held"), "无"),
        line("任务提示", guidance.get("quest"), "无"),
        "状态：" + ("战斗中" if guidance.get("in_combat") else "平静"),
        "",
        "引擎支持的动词（动作名=可用动词）：" + verbs,
        "",
        "硬性要求：",
        f"1. 恰好 {count} 条，整体只输出一个 JSON 数组，不要解释、不要代码块。",
        '2. 每条是一个对象：{"action": "...", "speech": "", "target": "..."}。',
        "3. action 必须以某个动词开头；需要目标时，目标名必须逐字取自上面列出的名字，不得自造地点、物品或人物。",
    ]
    # The three rules above are numbered literally, so the running counter has
    # to continue from 4 rather than from the row count (which counts labels,
    # blanks and the verb table too).
    rule_no = 4
    if allow_speech:
        rows.append(
            f"{rule_no}. 想和人物说话时，把台词原文放进 speech（不超过 40 字，"
            "不要写「向某人开口」这类叙述），target 填该人物名，action 留空；同一个人只出一条。"
        )
        rule_no += 1
    else:
        rows.append(f"{rule_no}. 本次不要生成「说话」类选项，每条都必须是一个动作。")
        rule_no += 1
    if not allow_investigate:
        rows.append(f"{rule_no}. 本次不要生成「检查／调查」类选项。")
        rule_no += 1
    rows.append(
        f"{rule_no}. 各条要互不相同，尽量指向不同目标；不要几条都是「观察四周」这类空泛动作。"
    )
    if extra_rules.strip():
        rows.append("作者追加的要求（与上面同等重要）：" + extra_rules.strip()[:600])
    samples = [{"action": "走向<出口名>", "speech": "", "target": ""}]
    if allow_speech:
        samples.append({"action": "", "speech": "<一句台词>", "target": "<人物名>"})
    if allow_investigate:
        samples.append({"action": "检查<某个物品>", "speech": "", "target": ""})
    samples.append({"action": "拿起<某个物品>", "speech": "", "target": ""})
    rows.append(
        "示例输出（只示范格式与写法，不要照抄里面的名字）："
        + json.dumps(samples[:count], ensure_ascii=False)
    )
    return _NEWLINE.join(rows)


def _tidy(item: Suggestion) -> Suggestion:
    """Drop the target chip when the action text already names it."""
    out = dict(item)
    target = _text(out.get("target"))
    action = _text(out.get("action"))
    if target and action and target in action:
        out["target"] = ""
        out.pop("target_id", None)
    return out


def intent_key(item: Suggestion, targets: Mapping[str, str] | None = None) -> tuple[str, str]:
    """What an option *does*, ignoring how it is phrased.

    Two ways of asking the same person the same thing must not both survive,
    so the key is the verb plus the thing it acts on, not the wording.
    """
    action = _text(item.get("action"))
    speech = _text(item.get("speech"))
    hit = _match(action) if action else None
    name = hit[1] if hit else ("talk" if speech else action.casefold())
    target = _text(item.get("target"))
    if not target and action and targets:
        names = _names_in(action, targets)
        target = names[0] if names else ""
    return name, target


def _dedupe_intents(
    items: Sequence[Suggestion],
    targets: Mapping[str, str] | None = None,
    limit: int = _LIMIT,
) -> list[Suggestion]:
    out: list[Suggestion] = []
    seen: set[tuple[str, str]] = set()
    for raw in items:
        item = _norm(raw)
        if not (item["action"] or item["speech"]):
            continue
        key = intent_key(item, targets)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def merge_options(
    primary: Sequence[Suggestion],
    fallback: Sequence[Suggestion],
    targets: Mapping[str, str] | None = None,
    limit: int = _LIMIT,
) -> list[Suggestion]:
    """Model options first, rule options filling any gap, `limit` at most."""
    merged = _dedupe_intents([*(primary or []), *(fallback or [])], targets, limit=limit)
    return [_tidy(item) for item in merged]


async def model_suggestions(
    router: Any,
    guidance: Mapping[str, Any],
    *,
    fallback: Sequence[Suggestion] = (),
    count: int = _LIMIT,
    timeout: float = 12.0,
    max_tokens: int = 400,
    temperature: float = 0.8,
    extra_rules: str = "",
    allow_speech: bool = True,
    allow_investigate: bool = True,
    use_story: bool = True,
    stats: dict[str, int] | None = None,
) -> list[Suggestion]:
    """The next steps written by the model, filtered to executable ones.

    router is the engine LLM router (the turn provider). Any failure -- no
    router, timeout, unparsable reply, every option rejected -- returns the
    rule fallback instead of raising, because a broken option list must never
    cost the player a turn.

    Every keyword here except `fallback` and `stats` is a panel setting, so a
    player can re-tune the option layer without touching code.
    """
    count = max(2, min(int(count or _LIMIT), 5))
    rule_options = [_tidy(item) for item in list(fallback or [])[:count]]
    if router is None:
        return rule_options
    targets = allowed_targets(guidance)
    if not targets:
        return rule_options
    messages = [
        {
            "role": "system",
            "content": (
                "你在为一款文字 RPG 生成玩家的下一步行动选项。"
                "你只能使用给定动词与给定名字，不得创造新地点、新物品或新人物。"
                "只输出 JSON 数组。"
            ),
        },
        {
            "role": "user",
            "content": guidance_prompt(
                guidance,
                count=count,
                extra_rules=extra_rules,
                allow_speech=allow_speech,
                allow_investigate=allow_investigate,
                use_story=use_story,
            ),
        },
    ]
    try:
        text = await asyncio.wait_for(
            router.complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning=False,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("model suggestions timed out after %.1fs - using rule options", timeout)
        return rule_options
    except Exception as exc:  # noqa: BLE001 - options are advisory
        logger.warning("model suggestions failed (%s: %s) - using rule options",
                       type(exc).__name__, exc)
        return rule_options

    options = parse_options(
        text or "",
        targets,
        _text(guidance.get("language")) or "zh",
        allow_speech=allow_speech,
        allow_investigate=allow_investigate,
    )
    if stats is not None:
        stats["model_options"] = len(options)
    if not options:
        logger.info("model suggestions rejected (%d chars) - using rule options", len(text or ""))
        return rule_options
    merged = merge_options(options, rule_options, targets, limit=count)
    logger.info("model suggestions: %d kept, %d from rules", len(options), len(merged) - len(options))
    return merged
