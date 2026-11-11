"""Isolate the FASE 2 cache MECHANISM from litellm's usage parsing.

Builds the exact cloaked payload Lunar produces (zone0/zone1 blocks with
cache_control ttl 1h in the first user message + extended-cache-ttl beta header),
then POSTs it straight to the CLIProxyAPI /v1/messages endpoint twice — bypassing
litellm — and reads the proxy's un-mangled rich usage.

If turn-2 cache_read_input_tokens ~ the zone prefix, the cloaking mechanism itself
caches on Anthropic (Claude Max). Any blindness in the litellm path is then a
telemetry bug, not a caching bug.

Run: backend/venv/Scripts/python scripts/direct_cache_probe.py [model] [proxy_key]
"""
from __future__ import annotations
import json
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.engines.narrator_engine import NarratorEngine, estimate_tokens

MODEL = sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-4-6"
PROXY_KEY = sys.argv[2] if len(sys.argv) > 2 else "onepiece-proxy-key"
URL = "http://127.0.0.1:8318/v1/messages"
CLOAK_TAG = "narrator-instructions"
CC = {"type": "ephemeral", "ttl": "1h"}


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


def build_messages(zone0: str, zone1: str, zone2: str, history: list[dict], player_input: str):
    cloaked = [
        {"type": "text", "text": f"<{CLOAK_TAG}>\n{zone0}\n</{CLOAK_TAG}>", "cache_control": CC},
        {"type": "text", "text": zone1, "cache_control": CC},
        {"type": "text", "text": zone2},
    ]
    return [{"role": "user", "content": cloaked}] + history + [{"role": "user", "content": player_input}]


def call(messages) -> dict:
    body = {"model": MODEL, "max_tokens": 280, "temperature": 0.7, "messages": messages}
    headers = {
        "x-api-key": PROXY_KEY,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "extended-cache-ttl-2025-04-11",
        "content-type": "application/json",
    }
    r = httpx.post(URL, headers=headers, json=body, timeout=120)
    r.raise_for_status()
    return r.json()


def main() -> int:
    narr = NarratorEngine(llm=None)
    zone0 = narr.build_zone0(tone_instructions=_big_tone(), language="en",
                             character_setup=_character_setup(), opening_narrative=_opening())
    zone1 = f"{_lore()}\n\nWORLD MEMORY (permanent canon):\n- Marek owes the Conclave a completed void-lane survey."
    zone2 = ("\n- HARD LENGTH LIMIT: keep it tight.\n\nPLAYER INVENTORY:\n- brass astrolabe (quest)\n"
             "\nACTIVE NPCs:\n- Conclave enforcer (watchful)")
    history = [
        {"role": "user", "content": "[DO] I step off the gangway onto the quay of Ost Verel."},
        {"role": "assistant", "content": "@Marek Vantsoll sets foot on the trembling quay, astrolabe clutched tight."},
    ]
    prefix_est = estimate_tokens(zone0) + estimate_tokens(zone1)
    print(f"model={MODEL}  cached-prefix est ~{prefix_est} tokens  (Z0~{estimate_tokens(zone0)} Z1~{estimate_tokens(zone1)})")

    m1 = build_messages(zone0, zone1, zone2, history,
                        "[SAY] Enforcer, what business does the Conclave have with a cartographer?")
    m2 = build_messages(zone0, zone1, zone2, history,
                        "[DO] I unfurl my storm-charts and trace the void-lane below the floor.")

    u1 = call(m1)["usage"]
    u2 = call(m2)["usage"]
    for tag, u in (("TURN 1", u1), ("TURN 2", u2)):
        print(f"\n[{tag}] {json.dumps(u)}")

    cr2 = u2.get("cache_read_input_tokens", 0)
    cc1 = u1.get("cache_creation_input_tokens", 0)
    print("\n" + "=" * 70)
    if cr2 >= 0.5 * prefix_est and cc1 >= 0.5 * prefix_est:
        print(f"MECHANISM PASS — turn-1 cache_creation={cc1}, turn-2 cache_read={cr2} "
              f"(~cached prefix {prefix_est}). The cloaked prefix caches on Anthropic.")
        rc = 0
    else:
        print(f"MECHANISM FAIL — turn-1 cache_creation={cc1}, turn-2 cache_read={cr2}, prefix~{prefix_est}.")
        rc = 1
    print("=" * 70)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
