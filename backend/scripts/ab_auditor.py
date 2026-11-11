"""FASE 3b A/B harness — measures the post-hoc Auditor (Camada 2).

The Auditor runs post-hoc over finished narrator prose, so the A/B is a PAIRED
comparison: narrator prose is generated once (fixed scenario + inputs), then each
response is run through AuditorEngine.audit(). Each pair (raw, audited) differs
ONLY by the audit — zero narrator sampling variance, unlike FASE 3a.

Two corpora, same inputs, same provider (DeepSeek V4 flash — FASE 0/3a baseline):
  - stress : narrator on OLD pink-elephant rules (tic-dense)  -> tests EFFICACY
  - prod   : narrator on NEW shipping rules (tic-sparse)       -> tests SAFETY / no-op

Outputs under docs/fase3b_ab/<corpus>/:
  raw.jsonl, audited.jsonl (parallel by idx), telemetry.json, regex_summary.json
Plus docs/fase3b_ab/summary.json (both corpora).

Env:
  CORPUS=stress|prod|both  (default both)
  LIMIT=<int>              (default all inputs; use 1 for a smoke test)

Run:  cd backend && PYTHONPATH=. python scripts/ab_auditor.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
logging.disable(logging.WARNING)

from app.config import settings

for _k, _v in [
    ("DEEPSEEK_API_KEY", settings.deepseek_api_key),
    ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
    ("OPENAI_API_KEY", settings.openai_api_key),
]:
    if _v and not os.environ.get(_k):
        os.environ[_k] = _v

from app.engines.llm_router import LLMRouter, LLMConfig
from app.engines.narrator_engine import NarratorEngine
from app.engines.auditor_engine import AuditorEngine, _item_fingerprint, _MENTION_RE

try:
    from tic_harness import (
        NEW_RULES, OLD_RULES, PLAYER_INPUTS, load_scenario_context,
        count_metrics, count_rule_of_three, count_em_dash_interruptive,
    )
except ImportError:
    from scripts.tic_harness import (
        NEW_RULES, OLD_RULES, PLAYER_INPUTS, load_scenario_context,
        count_metrics, count_rule_of_three, count_em_dash_interruptive,
    )

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_OUTDIR = os.path.join(_ROOT, "docs", "fase3b_ab")

NARRATOR_MAX_TOKENS = 1200
AUDIT_MAX_TOKENS = 3000  # generous: full rewrite + gate; truncation degrades to false-clean
LANGUAGE = "pt-br"

CORPORA = {"stress": OLD_RULES, "prod": NEW_RULES}


async def gen_raw(rules: dict, tone: str, cards_ctx: str, inputs: list[str]) -> list[dict]:
    cfg = LLMConfig()
    narr = NarratorEngine(LLMRouter(cfg))
    orig = NarratorEngine._NARRATOR_RULES
    NarratorEngine._NARRATOR_RULES = rules
    out, history = [], []
    try:
        for idx, pin in enumerate(inputs):
            sysp = narr.build_system_prompt(
                tone_instructions=tone,
                memory_context="",
                language=LANGUAGE,
                story_cards_context=cards_ctx,
                max_tokens=NARRATOR_MAX_TOKENS,
            )
            text = ""
            async for ch in narr.stream_narrative(
                pin, sysp, list(history), context_window=cfg.get_context_window()
            ):
                text += ch
            text = text.strip()
            out.append({"idx": idx, "input": pin, "text": text})
            history.append({"role": "user", "content": pin})
            history.append({"role": "assistant", "content": text})
            print(f"  gen {idx + 1}/{len(inputs)} len={len(text)}", flush=True)
    finally:
        NarratorEngine._NARRATOR_RULES = orig
    return out


async def audit_all(raws: list[dict], tone: str) -> list[dict]:
    auditor = AuditorEngine(LLMRouter(LLMConfig()))
    results = []
    for r in raws:
        t0 = time.perf_counter()
        final, report = await auditor.audit(
            prose=r["text"],
            player_input=r["input"],
            language=LANGUAGE,
            tone_instructions=tone,
            max_tokens=AUDIT_MAX_TOKENS,
        )
        dt = time.perf_counter() - t0
        results.append({
            "idx": r["idx"], "input": r["input"], "text": final,
            "report": report, "elapsed_s": round(dt, 2),
        })
        print(f"  audit {r['idx'] + 1}/{len(raws)} verdict={report.get('verdict')} "
              f"rewritten={report.get('prose_rewritten')} {dt:.1f}s", flush=True)
    return results


def tic_counts(text: str) -> dict:
    return {
        "rule_of_three": count_rule_of_three(text),
        "fake_metrics": count_metrics(text),
        "em_dash": count_em_dash_interruptive(text),
    }


def agg_regex(items: list[dict]) -> dict:
    n = len(items)
    tot = {"rule_of_three": 0, "fake_metrics": 0, "em_dash": 0}
    for it in items:
        c = tic_counts(it["text"])
        for k in tot:
            tot[k] += c[k]
    words = sum(len(it["text"].split()) for it in items)
    return {"n": n, "words": words, **tot,
            "per_resp": {k: round(tot[k] / max(1, n), 3) for k in tot}}


def agg_audit(audits: list[dict], raws: list[dict]) -> dict:
    n = len(audits)
    verdicts = Counter(a["report"].get("verdict", "?") for a in audits)
    rewritten = sum(1 for a in audits if a["report"].get("prose_rewritten"))
    guard_rej = sum(1 for a in audits if a["report"].get("marker_guard_rejected"))
    corr = Counter()
    for a in audits:
        for c in a["report"].get("corrections", []) or []:
            corr[c.get("rule_violated", "?")] += 1
    lat = [a["elapsed_s"] for a in audits] or [0.0]
    raw_by_idx = {r["idx"]: r["text"] for r in raws}
    tag_violations, mention_drops, len_deltas = 0, 0, []
    for a in audits:
        rw, fin = raw_by_idx[a["idx"]], a["text"]
        if _item_fingerprint(rw) != _item_fingerprint(fin):
            tag_violations += 1
        if len(_MENTION_RE.findall(fin)) < len(_MENTION_RE.findall(rw)):
            mention_drops += 1
        if a["report"].get("prose_rewritten"):
            len_deltas.append(len(fin) - len(rw))
    return {
        "n": n,
        "verdicts": dict(verdicts),
        "rewritten": rewritten,
        "rewrite_rate": round(rewritten / max(1, n), 3),
        "marker_guard_rejected": guard_rej,
        "corrections_total": sum(corr.values()),
        "corrections_by_rule": dict(corr.most_common()),
        "tag_invariant_violations": tag_violations,
        "mention_drops": mention_drops,
        "rewrite_len_delta_chars": {
            "mean": round(sum(len_deltas) / len(len_deltas), 1) if len_deltas else 0,
            "samples": len_deltas,
        },
        "latency_s": {"min": min(lat), "max": max(lat),
                      "mean": round(sum(lat) / len(lat), 2)},
    }


async def run_corpus(name: str, tone: str, cards_ctx: str, inputs: list[str]) -> dict:
    print(f"\n=== corpus {name} ({len(inputs)} inputs) ===", flush=True)
    raws = await gen_raw(CORPORA[name], tone, cards_ctx, inputs)
    audits = await audit_all(raws, tone)

    cdir = os.path.join(_OUTDIR, name)
    os.makedirs(cdir, exist_ok=True)
    with open(os.path.join(cdir, "raw.jsonl"), "w", encoding="utf-8") as f:
        for r in raws:
            f.write(json.dumps({"corpus": name, **r}, ensure_ascii=False) + "\n")
    with open(os.path.join(cdir, "audited.jsonl"), "w", encoding="utf-8") as f:
        for a in audits:
            f.write(json.dumps({"corpus": name, **a}, ensure_ascii=False) + "\n")

    regex = {"raw": agg_regex(raws), "audited": agg_regex(audits)}
    telem = agg_audit(audits, raws)
    with open(os.path.join(cdir, "regex_summary.json"), "w", encoding="utf-8") as f:
        json.dump(regex, f, ensure_ascii=False, indent=2)
    with open(os.path.join(cdir, "telemetry.json"), "w", encoding="utf-8") as f:
        json.dump(telem, f, ensure_ascii=False, indent=2)

    print(f"\n  [{name}] regex tics (raw -> audited, per resp):")
    for k in ("rule_of_three", "fake_metrics", "em_dash"):
        ro, ao = regex["raw"]["per_resp"][k], regex["audited"]["per_resp"][k]
        print(f"    {k:<16} {ro:.2f} -> {ao:.2f}")
    print(f"  [{name}] audit: verdicts={telem['verdicts']} rewrite_rate={telem['rewrite_rate']} "
          f"corrections={telem['corrections_by_rule']} guard_rej={telem['marker_guard_rejected']} "
          f"tag_violations={telem['tag_invariant_violations']} mention_drops={telem['mention_drops']}")
    return {"corpus": name, "regex": regex, "telemetry": telem}


async def main() -> None:
    os.makedirs(_OUTDIR, exist_ok=True)
    tone, cards_ctx = load_scenario_context()
    limit = int(os.environ.get("LIMIT", "0")) or None
    inputs = PLAYER_INPUTS[:limit] if limit else PLAYER_INPUTS
    which = os.environ.get("CORPUS", "both").lower()
    names = ["stress", "prod"] if which == "both" else [which]

    print(f"tone={len(tone)} chars, cards_ctx={len(cards_ctx)} chars, "
          f"inputs={len(inputs)}, corpora={names}", flush=True)

    summary = {}
    for name in names:
        summary[name] = await run_corpus(name, tone, cards_ctx, inputs)
    with open(os.path.join(_OUTDIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\nDONE — outputs in docs/fase3b_ab/")


if __name__ == "__main__":
    asyncio.run(main())
