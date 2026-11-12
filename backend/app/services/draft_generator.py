"""Free-form draft generation for authoring UIs.

The character library and the scenario builder both want the same thing: "write
me a first draft of this field". This module owns the one prompt path so the two
stay consistent, and it degrades the same way everything else does — with no key
configured it returns a deterministic template draft instead of an error, so
authoring never blocks on a model.
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


def _normalise(value: str) -> str:
    return " ".join((value or "").split())


def template_draft(kind: str, topic: str = "") -> str:
    """The no-model fallback. Exposed so tests can pin it."""
    return _TEMPLATES.get(kind, "{topic}").format(topic=_normalise(topic) or "这个地方")


async def generate_draft(
    kind: str,
    *,
    topic: str = "",
    context: str = "",
    language: str = "zh",
    router: LLMRouter | None = None,
) -> tuple[str, str]:
    """Return a (draft_text, provider) pair; provider is "llm" or "template"."""
    kind = (kind or "").strip().lower()
    if kind not in DRAFT_KINDS:
        raise ValueError("unsupported kind: " + kind)

    topic = _normalise(topic)
    context = (context or "").strip()
    lang = (language or "zh").strip()[:10] or "zh"

    if router is None:
        try:
            router = LLMRouter(LLMConfig())
        except Exception:  # noqa: BLE001 - authoring must work without a model
            logger.warning("draft generation falls back to template: router unavailable")
            return template_draft(kind, topic), "template"

    lang_name = "Chinese (Simplified)" if lang.startswith("zh") else "English"
    parts = [
        "LANGUAGE: write everything in " + lang_name + ".",
        "KIND: " + kind + " — " + DRAFT_KINDS[kind] + ".",
        "RULES: " + _KIND_RULES.get(kind, DRAFT_KINDS[kind]),
    ]
    if topic:
        parts.append("NAME / TOPIC: " + topic)
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
