"""Draft a work profile with the model when the local catalogue has no match.

Kept beside the catalogue rather than inside it so the curated data stays
plain Python with no LLM import at module load.
"""
from __future__ import annotations

from typing import Optional

from app.models.work import WorkProfile
from app.services import ai_generator


SYSTEM = (
    "你是文字 RPG 的世界考据编辑。只输出 JSON，不要 markdown。"
    "只写关于该作品的公开设定，不编造不存在的主要角色；不确定的细节宁可省略。"
)


def build_prompt(title: str, language: str = "中文") -> str:
    return f"""为作品《{title}》整理一份可用于开局的世界档案，语言：{language}。

输出 JSON：{{"title":"...","medium":"novel|game|film|anime|myth","summary":"两三句话的世界概述",
"tone":"叙事语气，一句话","locations":["3-6 个故事发生的地点名"],
"opening_hint":"一句开局提示，写玩家所处的瞬间",
"characters":[{{"name":"...","role":"...","description":"一句话人物画像","traits":["能力或性格标签，2-4 个"]}}]}}

要求：至少 3 个角色、至少 3 个地点；角色必须是该作品里真实存在的；
描述里不写数值、不写游戏机制、不写「你可以做任何事」。"""


async def generate_work(router, title: str, language: str = "中文") -> WorkProfile:
    payload = await ai_generator._ask(router, SYSTEM, build_prompt(title, language))
    return WorkProfile.from_payload(payload, title=title, source="generated")


async def resolve_work(router, title: str, language: str = "中文") -> WorkProfile:
    """Curated data first; the model is only a fallback."""
    from app.services import work_catalogue

    local = work_catalogue.find_work(title)
    if local is not None:
        return local
    return await generate_work(router, title, language)