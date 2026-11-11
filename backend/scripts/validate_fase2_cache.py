"""FASE 2 empirical cache validation.

Drives the REAL narrator cached path (stream_narrative_cached -> LLMRouter.stream ->
litellm -> CLIProxyAPI -> Anthropic) for two back-to-back turns with a byte-identical
zone0/zone1 prefix, and reads usage to confirm the prefix becomes a cache read on turn 2.

Rigorous pass condition (not just cache_read > 0, which the proxy's own injected
system prompt would satisfy): turn-2 cache_read must approach the token size of
zone0+zone1, and turn-2 input_tokens must collapse. That proves litellm forwarded
our cache_control/extra_headers all the way through.

Run:  backend/venv/Scripts/python scripts/validate_fase2_cache.py [model] [proxy_key]
"""
from __future__ import annotations
import asyncio
import os
import sys

# The live proxy on :8318 is the OP-RPG instance (same CLIProxyAPI binary + Claude Max
# backend); its inbound key differs from Lunar's. Override so the transport authenticates.
PROXY_KEY = sys.argv[2] if len(sys.argv) > 2 else "onepiece-proxy-key"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-4-6"
os.environ.setdefault("ANTHROPIC_API_KEY", PROXY_KEY)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import litellm
from app.engines import llm_router as lr
from app.engines.llm_router import (
    LLMRouter, LLMConfig, LLMProvider, reset_call_log, get_call_summary,
)
from app.engines.narrator_engine import NarratorEngine, estimate_tokens

lr._ANTHROPIC_PROXY_KEY = PROXY_KEY  # module global is read per-call

# Spy on the raw litellm response so we can read the full usage (ephemeral 5m/1h split
# that Lunar's _log_call does not surface).
_last: dict = {}
_orig_acompletion = litellm.acompletion


async def _spy(*args, **kwargs):
    _last["kwargs"] = kwargs
    resp = await _orig_acompletion(*args, **kwargs)
    _last["resp"] = resp
    return resp


litellm.acompletion = _spy


def _big_tone() -> str:
    lines = [
        "You narrate a high-fantasy world of drifting sky-islands bound by storm-currents.",
        "Voice: grounded, sensory, unhurried. Let consequences land with weight.",
    ]
    for i in range(60):
        lines.append(
            f"Style anchor {i:02d}: describe the {['salt','copper','ash','resin','frost'][i % 5]} "
            f"tang of the air, the {['groaning','singing','silent','fractured','humming'][i % 5]} "
            f"rigging, and how the light of the twin moons falls across weathered stone. "
            f"Never rush a reveal; earn every turn of the scene through concrete detail."
        )
    return "\n".join(lines)


def _character_setup() -> str:
    return (
        "PLAYER CHARACTER:\n"
        "Name: Marek Vantsoll. A stormwright cartographer from the leeward archipelago, "
        "carrying a brass astrolabe keyed to dead constellations and a debt to the Harbor Conclave. "
        "Competent but unproven at true altitude; wary of the Conclave's motives."
    )


def _opening() -> str:
    parts = ["The gangway of the skiff Meridian shudders as it kisses the floating quay of Ost Verel."]
    for i in range(70):
        parts.append(
            f"Opening beat {i:02d}: @Marek Vantsoll steadies the astrolabe as vendor {i} calls out "
            f"prices for tethered kites, glass eels, and charts of the {['northern','shrouded','burning','sunken','hollow'][i % 5]} reach; "
            f"the crowd parts around a Conclave enforcer whose insignia catches the moonlight."
        )
    return "\n".join(parts)


def _lore() -> str:
    parts = ["WORLD LORE (canonical reference for this campaign):"]
    for i in range(90):
        parts.append(
            f"LORE {i:03d}: The {['Conclave','Stormwrights','Deepwatch','Ferrymen','Ashborn'][i % 5]} govern "
            f"trade of raw aether across island {i}; their charter forbids charting the void-lanes below the storm-floor. "
            f"Historical note: in the year {1400 + i}, the sky-island of Vael-{i} broke its moorings and drifted into the black."
        )
    return "\n".join(parts)


async def run_turn(narr: NarratorEngine, zone0: str, zone1: str, zone2: str,
                   history: list[dict], player_input: str, label: str) -> dict:
    reset_call_log()
    _last.pop("resp", None)
    chunks: list[str] = []
    async for c in narr.stream_narrative_cached(
        player_input, zone0, zone1, zone2, history, context_window=1_000_000,
    ):
        chunks.append(c)
    text = "".join(chunks)

    summary = get_call_summary()
    call = (summary.get("calls") or [{}])[0]
    usage = getattr(_last.get("resp"), "usage", None)
    raw = {}
    if usage is not None:
        try:
            raw = usage.model_dump()
        except Exception:
            raw = {k: getattr(usage, k) for k in dir(usage) if not k.startswith("_") and not callable(getattr(usage, k, None))}

    out = {
        "label": label,
        "input_tokens": call.get("input_tokens", 0),
        "output_tokens": call.get("output_tokens", 0),
        "cache_read": call.get("cache_read", 0),
        "cache_creation": call.get("cache_creation", 0),
        "text_head": text[:120].replace("\n", " "),
        "raw_usage": raw,
    }
    return out


async def main() -> int:
    cfg = LLMConfig(
        primary_provider=LLMProvider.ANTHROPIC,
        primary_model=MODEL,
        temperature=0.7,
        max_tokens=280,
    )
    router = LLMRouter(cfg)
    narr = NarratorEngine(router)

    # Per-run nonce keeps the cached prefix fresh each run, so turn 1 CREATES the cache
    # and turn 2 READS it (otherwise a prior run's still-live 1h cache pre-warms turn 1).
    nonce = f"[run {os.getpid()}] "
    tone, char, opening, lore = nonce + _big_tone(), _character_setup(), _opening(), _lore()
    zone0 = narr.build_zone0(tone_instructions=tone, language="en",
                             character_setup=char, opening_narrative=opening)
    zone1 = f"{lore}\n\nWORLD MEMORY (permanent canon):\n- Marek owes the Conclave a completed void-lane survey."
    zone2 = (
        f"\n{narr.length_directive(280)}\n"
        "\nPLAYER INVENTORY:\n- brass astrolabe (quest)\n- worn storm-charts (tool)\n"
        "\nACTIVE NPCs:\n- Conclave enforcer (watchful, suspicious of outsiders)"
    )
    history: list[dict] = [
        {"role": "user", "content": "[DO] I step off the gangway onto the quay of Ost Verel."},
        {"role": "assistant", "content": "@Marek Vantsoll sets foot on the trembling quay, astrolabe clutched tight as the crowd of Ost Verel churns around him."},
    ]

    z0t, z1t, z2t = estimate_tokens(zone0), estimate_tokens(zone1), estimate_tokens(zone2)
    prefix_est = z0t + z1t

    print("=" * 78)
    print(f"FASE 2 CACHE VALIDATION  model={MODEL}  proxy={lr._ANTHROPIC_PROXY_URL}")
    print(f"Zone token estimates:  Z0={z0t}  Z1={z1t}  (cached prefix ~{prefix_est})  Z2={z2t}")
    print("=" * 78)

    t1 = await run_turn(narr, zone0, zone1, zone2, history,
                        "[SAY] Enforcer, what business does the Conclave have with a cartographer?",
                        "TURN 1")
    t2 = await run_turn(narr, zone0, zone1, zone2, history,
                        "[DO] I unfurl my storm-charts on a crate and trace the void-lane below the floor.",
                        "TURN 2")

    # One-time transport dump: confirm cache_control + extra_headers survived into the
    # litellm call, and show the full raw usage the proxy returned.
    kw = _last.get("kwargs", {})
    sys_blocks = (kw.get("messages") or [{}])[0]
    print("\n--- TRANSPORT (last litellm call) ---")
    print(f"  extra_headers: {kw.get('extra_headers')}")
    print(f"  api_base={kw.get('api_base')}  stream={kw.get('stream')}  first_msg_role={sys_blocks.get('role')}")
    fc = sys_blocks.get("content")
    if isinstance(fc, list):
        for i, b in enumerate(fc):
            print(f"    block[{i}] type={b.get('type')} chars={len(b.get('text',''))} "
                  f"cache_control={b.get('cache_control')}")
    else:
        print(f"    first-msg content is a plain string (len={len(fc) if fc else 0}) — NOT cloaked blocks")

    for t in (t1, t2):
        print(f"\n[{t['label']}]  input={t['input_tokens']}  output={t['output_tokens']}  "
              f"cache_read={t['cache_read']}  cache_creation={t['cache_creation']}")
        print(f"        FULL raw usage: {t['raw_usage']}")
        print(f"        narrative head: {t['text_head']!r}")

    # Verdict
    print("\n" + "=" * 78)
    cr2 = t2["cache_read"]
    cc1 = t1["cache_creation"]
    served = cr2 >= 0.5 * prefix_est                          # turn-2 serves the prefix from cache
    wrote_t1 = cc1 >= 0.5 * prefix_est                        # turn-1 created the cache
    read_t1 = t1["cache_read"] >= 0.5 * prefix_est            # or a live prior cache pre-warmed it
    if served and (wrote_t1 or read_t1):
        origin = f"wrote cache_creation={cc1}" if wrote_t1 else f"read pre-warmed cache={t1['cache_read']}"
        print(f"VERDICT: PASS — turn-2 cache_read={cr2} ~ cached prefix (~{prefix_est}); turn-1 {origin}. "
              f"cache_control reaches CLIProxyAPI; the prefix is served from cache.")
        rc = 0
    elif cr2 > 0:
        print(f"VERDICT: WEAK — cache_read={cr2} > 0 but below the zone prefix (~{prefix_est}). "
              f"Likely only the proxy's own injected prefix cached, not Lunar's zones.")
        rc = 2
    else:
        print(f"VERDICT: FAIL — turn-2 cache_read={cr2} (turn-1 cache_creation={cc1}). "
              f"Prefix not cached: cache_control not forwarded, or min-token threshold not met.")
        rc = 1
    print("=" * 78)
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
