"""Free-form draft generation for authoring UIs.

The character library and the scenario builder both want the same thing: "write
me a first draft of this field". This module owns the one prompt path so the two
stay consistent, and it degrades the same way everything else does — with no key
configured it returns a deterministic template draft instead of an error, so
authoring never blocks on a model.

Two optional enrichments hang off the same path:

* `search=True` looks the topic up on the web first and hands the model a short
  fact block. A real name (a historical figure, a myth, a game) then produces a
  draft grounded in something rather than the model's memory. No search hit, or
  no network, simply means an un-grounded draft.
* `expand()` takes what the author already wrote and continues it, instead of
  replacing it — the usual second step after a first draft feels too thin.
"""
from __future__ import annotations

import logging

from app.engines.llm_router import LLMConfig, LLMRouter

logger = logging.getLogger(__name__)

#: Kinds the frontend may ask for. Anything else is rejected, so this list is
#: also the contract between the two sides.
DRAFT_KINDS = {
    "character": "a player character or an NPC",
    "world": "a world / scenario",
    "location": "a place inside a world",
    "faction": "an organisation or faction",
    "item": "an object, relic or weapon",
    "lore": "a piece of world lore",
}

_SYSTEM = (
    "You are a worldbuilding assistant for a text adventure engine. "
    "Answer with the drafted content only: no preamble, no headings, no "
    "bullet lists, no explanation of what you did. Plain prose in the "
    "requested language."
)

_SYSTEM_EXPAND = (
    "You are a worldbuilding assistant for a text adventure engine. The author "
    "already wrote some text and wants it continued, not rewritten. Reply with "
    "ONLY the continuation as plain prose in the requested language: no "
    "repeating the existing text, no headings, no bullet lists, no commentary."
)

_KIND_RULES = {
    "character": (
        "Write a character in 3-5 sentences: appearance, temperament, what they "
        "want and what they hide. Do not state game stats."
    ),
    "world": (
        "Write the world background in 4-6 sentences: era, geography, the "
        "pressure everyone lives under, and one concrete detail that makes it "
        "feel lived in."
    ),
    "location": (
        "Describe the place in 2-4 sentences: what it looks like, what happens "
        "there, what a visitor notices first."
    ),
    "faction": (
        "Describe the organisation in 3-5 sentences: who leads it, how it "
        "recruits, what it wants, who it hurts."
    ),
    "item": (
        "Describe the object in 2-4 sentences: what it is, what it does, what it "
        "costs to use."
    ),
    "lore": (
        "Write a piece of lore in 3-5 sentences: what people believe, what "
        "actually happened, and the detail that betrays the difference."
    ),
}

_EXPAND_RULES = {
    "character": "Add 2-4 more sentences: a habit, a relationship, and the cost of what they want.",
    "world": "Add 2-4 more sentences: a second region, a faction, and a rumour the locals repeat.",
    "location": "Add 2-3 more sentences: the smell, the sound, and who is here at this hour.",
    "faction": "Add 2-3 more sentences: its sigil, its fear, and the oath members swear.",
    "item": "Add 2-3 more sentences: its origin, and the story of who last held it.",
    "lore": "Add 2-3 more sentences: a contradiction, and one witness who insists otherwise.",
}

#: Deterministic drafts keep authoring usable with no model configured. They are
#: deliberately generic-but-structured rather than plausible fiction.
_TEMPLATES = {
    "character": (
        "{topic}是一个来历不明的人。身形寻常，目光却总比旁人多停半拍，像是在估算什么。"
        "说话简短，喜欢把决定留到最后一刻。嘴上说只为了活下去，可每次都会为不相干的人多走一步"
        "——他对「被记住」这件事的恐惧，远大于对死亡的恐惧。"
    ),
    "world": (
        "{topic}：这里的边界由旧约划定，越过的人不会死，只会被所有人遗忘。"
        "资源集中在少数渡口，其余的人在废墟间迁徙，靠回收上一轮文明留下的器具过活。"
        "孩子长大前要背熟一份名单，名单上的名字不许提。"
    ),
    "location": (
        "{topic}常年被水汽覆盖，石板路上留着几百年前的车辙。"
        "白天是集市，入夜后只剩守夜人的灯。"
        "来客最先注意到的，是墙上层层叠叠的钩子——它们从不落空。"
    ),
    "faction": (
        "{topic}自称一所学派，实际上更像个账房。"
        "他们不统治任何人，只是替所有势力记账，并且从不主动销账。"
        "入伙需要交出一段私人记忆，退出则需要有人替你还上。"
    ),
    "item": (
        "{topic}看起来平平无奇，握久了会微微发热。"
        "它能让人记起一段本不属于自己的往事，代价是忘掉一件自己的事"
        "——用的人越多，就越记不清自己为什么开始用。"
    ),
    "lore": (
        "关于{topic}的说法有三种版本，彼此矛盾却同时被相信。"
        "真实的情况记录在一份没人愿意承认的文书里：所有版本都是从同一段错误上长出来的。"
    ),
}

_EXPAND_TEMPLATES = {
    "character": "他有个改不掉的习惯：说谎前会先整理袖口。唯一的亲人在渡口管账，两人三年没说过话。",
    "world": "南边的盐场由一群不肯报名的矿工作主，他们只信一句老话：记得你的人越少，你活得越久。",
    "location": "空气里有淡淡的铁锈味，脚步声会被石板吃掉一半。这个时辰留在这里的，只有贩药的女人。",
    "faction": "他们的印记是一枚倒着写的数字。他们最怕的不是敌人，而是账目里突然多出一笔平不了的条目。",
    "item": "据说它出自一个不会被记录的工匠之手。上一个持有人在把它埋掉之前，写下过三个自己的名字。",
    "lore": "只有渡口的老船夫坚持另一种说法：名单从来不存在，是一代代人彼此劝服的结果。",
}


def _normalise(value: str) -> str:
    return " ".join((value or "").split())


def template_draft(kind: str, topic: str = "") -> str:
    """The no-model fallback. Exposed so tests can pin it."""
    return _TEMPLATES.get(kind, "{topic}").format(topic=_normalise(topic) or "这个地方")


def template_expansion(kind: str) -> str:
    """The no-model continuation fallback."""
    return _EXPAND_TEMPLATES.get(kind, _EXPAND_TEMPLATES["world"])


def _language_name(language: str) -> str:
    return "Chinese (Simplified)" if (language or "zh").strip().lower().startswith("zh") else "English"


async def _build_router(router: LLMRouter | None) -> LLMRouter | None:
    if router is not None:
        return router
    try:
        return LLMRouter(LLMConfig())
    except Exception:  # noqa: BLE001 - authoring must work without a model
        logger.warning("draft generation falls back to template: router unavailable")
        return None


async def _search_notes(topic: str, kind: str) -> tuple[str, list[dict]]:
    """Return (notes block, raw hits); both empty when search is not usable."""
    if not topic:
        return "", []
    from app.services.web_search import format_notes, web_search

    query = topic
    if kind in ("world", "location", "faction", "lore", "item"):
        query = topic + " 设定 背景"
    hits = await web_search(query)
    if not hits:
        return "", []
    return format_notes(hits), hits


async def generate_draft(
    kind: str,
    *,
    topic: str = "",
    context: str = "",
    language: str = "zh",
    router: LLMRouter | None = None,
    search: bool = False,
) -> tuple[str, str]:
    """Return a (draft_text, provider) pair; provider is "llm" or "template"."""
    kind = (kind or "").strip().lower()
    if kind not in DRAFT_KINDS:
        raise ValueError("unsupported kind: " + kind)

    topic = _normalise(topic)
    context = (context or "").strip()
    lang = (language or "zh").strip()[:10] or "zh"

    notes = ""
    if search:
        notes, _hits = await _search_notes(topic, kind)

    router = await _build_router(router)
    if router is None:
        return template_draft(kind, topic), "template"

    parts = [
        "LANGUAGE: write everything in " + _language_name(lang) + ".",
        "KIND: " + kind + " — " + DRAFT_KINDS[kind] + ".",
        "RULES: " + _KIND_RULES.get(kind, DRAFT_KINDS[kind]),
    ]
    if topic:
        parts.append("NAME / TOPIC: " + topic)
    if notes:
        parts.append(
            "WEB NOTES (real-world facts about the topic; follow them where they "
            "apply and keep invented details consistent with them):\n" + notes
        )
    if context:
        parts.append("EXISTING CONTEXT (stay consistent with it):\n" + context[:4000])
    parts.append("Write the draft now.")

    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
    try:
        text = await router.complete(messages, max_tokens=500, temperature=0.95)
    except Exception as exc:  # noqa: BLE001 - a dead key must not block authoring
        logger.warning("draft generation failed (%s), using template", exc)
        return template_draft(kind, topic), "template"

    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return template_draft(kind, topic), "template"
    return cleaned, "llm"


async def expand_draft(
    kind: str,
    *,
    text: str,
    topic: str = "",
    language: str = "zh",
    router: LLMRouter | None = None,
    search: bool = False,
) -> tuple[str, str]:
    """Continue existing author text. Returns (continuation, provider)."""
    kind = (kind or "").strip().lower()
    if kind not in DRAFT_KINDS:
        raise ValueError("unsupported kind: " + kind)
    existing = (text or "").strip()
    if not existing:
        return template_expansion(kind), "template"
    topic = _normalise(topic)
    lang = (language or "zh").strip()[:10] or "zh"

    notes = ""
    if search and topic:
        notes, _hits = await _search_notes(topic, kind)

    router = await _build_router(router)
    if router is None:
        return template_expansion(kind), "template"

    parts = [
        "LANGUAGE: write everything in " + _language_name(lang) + ".",
        "KIND: " + kind + " — " + DRAFT_KINDS[kind] + ".",
        "WHAT TO ADD: " + _EXPAND_RULES.get(kind, _EXPAND_RULES["world"]),
    ]
    if topic:
        parts.append("NAME / TOPIC: " + topic)
    if notes:
        parts.append("WEB NOTES (real-world facts; stay consistent with them):\n" + notes)
    parts.append("TEXT SO FAR:\n" + existing[-4000:])
    parts.append("Continue the text from where it stops.")

    messages = [
        {"role": "system", "content": _SYSTEM_EXPAND},
        {"role": "user", "content": "\n".join(parts)},
    ]
    try:
        text_out = await router.complete(messages, max_tokens=400, temperature=0.95)
    except Exception as exc:  # noqa: BLE001 - a dead key must not block authoring
        logger.warning("draft expansion failed (%s), using template", exc)
        return template_expansion(kind), "template"

    cleaned = " ".join((text_out or "").strip().split())
    if not cleaned:
        return template_expansion(kind), "template"
    return cleaned, "llm"
