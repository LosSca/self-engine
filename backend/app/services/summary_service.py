"""LLM-backed compression of the rolling campaign recap.

The deterministic recap in ActionService is always available. When a scene
configures a summary preset, this module periodically replaces it with a
shorter model-written recap. Failure is silent: the deterministic text stays.
The model only reads already-committed prose; it cannot add facts.
"""
from __future__ import annotations

import asyncio

SUMMARY_EVERY_TURNS = 4
SUMMARY_MAX_CHARS = 1200


async def summarize(router, recap: str, timeout: float = 20.0) -> str:
    if router is None or not (recap or "").strip():
        return ""
    messages = [
        {
            "role": "system",
            "content": (
                "Summarize the committed RPG log into at most 8 short lines. "
                "Keep only facts already present in the log; never invent events, "
                "items, relationships or outcomes. Write in the log's language."
            ),
        },
        {"role": "user", "content": recap[-4000:]},
    ]
    try:
        text = await asyncio.wait_for(
            router.complete(messages, max_tokens=400, temperature=0.2),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - memory is advisory, never fatal
        return ""
    cleaned = " ".join((text or "").split()).strip()
    if not cleaned:
        return ""
    return cleaned[:SUMMARY_MAX_CHARS]
