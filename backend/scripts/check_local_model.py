r"""Smoke-check the local NInfer server through the same code path the game uses.

Run from the backend directory:
    .\.venv\Scripts\python.exe scripts\check_local_model.py

It only reads: /v1/models, one completion, one streamed completion, and the
/api/v1/local-model diagnostic. Exit code 0 means the local provider is wired.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import routes_engine  # noqa: E402
from app.config import settings  # noqa: E402
from app.engines.llm_router import LLMConfig, LLMProvider, LLMRouter  # noqa: E402

PROMPT = (
    "You are the narrator of a text RPG. The player just unlocked a warehouse door "
    "with an iron key. In two sentences, describe the moment."
)


async def main() -> int:
    print(f"base_url : {settings.local_llm_base_url}")
    print(f"model    : {settings.local_llm_model}")
    print(f"key set  : {bool(settings.local_llm_api_key)}")

    status = await routes_engine.local_model_status()
    print(f"reachable: {status.reachable}  models={status.available_models}")
    if not status.reachable:
        print(f"detail   : {status.detail}")
        return 1

    router = LLMRouter(
        LLMConfig(
            primary_provider=LLMProvider.LOCAL,
            primary_model=settings.local_llm_model,
            temperature=0.7,
            max_tokens=160,
        )
    )
    text = await router.complete([{"role": "user", "content": PROMPT}], max_tokens=160)
    print(f"complete : {text.strip()[:400]}")

    parts = [chunk async for chunk in router.stream([{"role": "user", "content": PROMPT}])]
    streamed = "".join(parts).strip()
    print(f"stream   : {streamed[:400]}")
    return 0 if text.strip() and streamed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
