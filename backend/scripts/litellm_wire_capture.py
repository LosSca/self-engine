"""Capture the exact HTTP body litellm sends to the proxy, to determine whether
litellm 1.43.0 forwards Lunar's cache_control / anthropic-beta on the anthropic+api_base path.

If the outgoing body carries cache_control on the zone blocks, then production caching
works and the litellm usage-parsing blindness is telemetry-only. If cache_control is
stripped, production does NOT cache and the [USAGE] readout is truthfully reporting 0.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ANTHROPIC_API_KEY", "onepiece-proxy-key")

from app.engines import llm_router as lr
from app.engines.llm_router import LLMRouter, LLMConfig, LLMProvider
from app.engines.narrator_engine import NarratorEngine

lr._ANTHROPIC_PROXY_KEY = "onepiece-proxy-key"

cap: dict = {}
_orig = httpx.AsyncClient.send


async def _send(self, request, **kw):
    try:
        cap["url"] = str(request.url)
        cap["headers"] = {k.lower(): v for k, v in request.headers.items()}
        body = request.content
        cap["body"] = body.decode("utf-8", "replace") if body else ""
    except Exception as e:  # noqa
        cap["err"] = repr(e)
    return await _orig(self, request, **kw)


httpx.AsyncClient.send = _send


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "cloak"
    import litellm
    if mode == "system":
        # Does litellm preserve cache_control when it sits on a `system` block
        # (the standard prompt-caching placement) rather than a cloaked user block?
        big = "WORLD LORE:\n- The Conclave rules the sky-lanes.\n" * 40
        try:
            await litellm.acompletion(
                model="anthropic/claude-sonnet-4-6",
                api_base=lr._ANTHROPIC_PROXY_URL, api_key=lr._ANTHROPIC_PROXY_KEY,
                max_tokens=32, stream=False,
                extra_headers={"anthropic-beta": "extended-cache-ttl-2025-04-11"},
                messages=[
                    {"role": "system", "content": [
                        {"type": "text", "text": big, "cache_control": {"type": "ephemeral", "ttl": "1h"}}]},
                    {"role": "user", "content": "hi"},
                ],
            )
        except Exception as e:  # noqa
            print(f"(call error, wire still captured): {e!r}")
    else:
        router = LLMRouter(LLMConfig(primary_provider=LLMProvider.ANTHROPIC,
                                     primary_model="claude-sonnet-4-6",
                                     temperature=0.7, max_tokens=48))
        narr = NarratorEngine(router)
        zone0 = narr.build_zone0(tone_instructions="TONE: terse and vivid.\n" * 6,
                                 language="en", character_setup="PC: Marek.",
                                 opening_narrative="Marek stands on the quay.")
        zone1 = "WORLD LORE:\n- The Conclave rules the sky-lanes.\n" * 4
        zone2 = "\nPLAYER INVENTORY:\n- astrolabe"
        async for _ in narr.stream_narrative_cached("[SAY] hello", zone0, zone1, zone2, [],
                                                    context_window=1_000_000):
            pass
    print(f"[mode={mode}]")

    body = cap.get("body", "")
    parsed = {}
    try:
        parsed = json.loads(body)
    except Exception:
        pass

    print(f"URL: {cap.get('url')}")
    print(f"anthropic-beta header on wire: {cap.get('headers', {}).get('anthropic-beta')!r}")
    print(f"x-api-key present: {'x-api-key' in cap.get('headers', {})}")
    n_cc = body.count('"cache_control"')
    has_1h = '"1h"' in body
    print(f"'cache_control' occurrences in outgoing body: {n_cc}")
    print(f"'ttl' 1h in body: {has_1h}")
    print(f"cloak tag in body: {'narrator-instructions' in body}")

    # Show where cache_control sits in the transformed payload (system vs messages)
    if parsed:
        sysf = parsed.get("system")
        print(f"\npayload.system type: {type(sysf).__name__}")
        if isinstance(sysf, list):
            for i, b in enumerate(sysf):
                print(f"  system[{i}] cache_control={b.get('cache_control')} chars={len(str(b.get('text','')))}")
        msgs = parsed.get("messages", [])
        print(f"payload.messages: {len(msgs)}")
        for i, m in enumerate(msgs):
            c = m.get("content")
            if isinstance(c, list):
                marks = [blk.get("cache_control") for blk in c if isinstance(blk, dict)]
                print(f"  messages[{i}] role={m.get('role')} blocks={len(c)} cache_control={marks}")
            else:
                print(f"  messages[{i}] role={m.get('role')} content=str(len={len(str(c))})")

    print("\n" + "=" * 70)
    if n_cc >= 1:
        print("FORWARDING PASS — litellm keeps cache_control on the wire. Production caches; "
              "the [USAGE] readout is blind only because litellm mangles the RETURNED usage.")
        rc = 0
    else:
        print("FORWARDING FAIL — litellm stripped cache_control before the proxy. "
              "Production does NOT cache on this path; plan B (anthropic SDK direct) is required.")
        rc = 1
    print("=" * 70)
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
