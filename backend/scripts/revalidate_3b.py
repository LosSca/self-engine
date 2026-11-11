"""Re-validate the context-aware Auditor (FASE 3b fix) on the SAME BEFORE corpus.

Loads docs/fase3b_ab/{stress,prod}/raw.jsonl (prose from the BEFORE run) and re-audits
each response with the NEW context-aware auditor (recent_scene + world_context), then
diffs verdict/rewrite/corrections against the BEFORE audited.jsonl. This isolates the
auditor change: identical input prose, old-auditor vs new-auditor. Writes the after
reports to docs/fase3b_ab/{corpus}/audited_after.jsonl.

Key acceptance check: prod idx16 (the context-blind electricity excision) must no longer
drop the established ability.

Run:  cd backend && CORPUS=both PYTHONPATH=. python scripts/revalidate_3b.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

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
from app.engines.auditor_engine import AuditorEngine

try:
    from tic_harness import load_scenario_context
except ImportError:
    from scripts.tic_harness import load_scenario_context

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DIR = os.path.join(_ROOT, "docs", "fase3b_ab")
LANGUAGE = "pt-br"
AUDIT_MAX_TOKENS = 3000
RECENT_TURNS = 6  # prior turns fed as recent_scene (mirrors _render_recent_scene tail)
ELECTRIC_MARKERS = ("eletric", "faísca", "faisca", "crepit", "descarga", "centelh")


def _load(corpus: str, name: str) -> list[dict]:
    path = os.path.join(_DIR, corpus, f"{name}.jsonl")
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def _render_recent(raws: list[dict], i: int) -> str:
    lines = []
    for r in raws[max(0, i - RECENT_TURNS):i]:
        lines.append(f"PLAYER: {r['input']}\n\nNARRATOR: {r['text']}")
    return "\n\n".join(lines)


def _rules(report: dict) -> list[str]:
    return [c.get("rule_violated") for c in (report.get("corrections") or [])]


async def run_corpus(corpus: str, auditor: AuditorEngine, cards_ctx: str, tone: str) -> None:
    raws = _load(corpus, "raw")
    before = {a["idx"]: a["report"] for a in _load(corpus, "audited")}
    afters = []
    print(f"\n{'=' * 78}\nCORPUS {corpus}\n{'idx':<5}{'BEFORE':<34}{'AFTER':<34}")
    for i, r in enumerate(raws):
        idx = r["idx"]
        recent = _render_recent(raws, i)
        t0 = time.perf_counter()
        final, report = await auditor.audit(
            prose=r["text"], player_input=r["input"], language=LANGUAGE,
            tone_instructions=tone, max_tokens=AUDIT_MAX_TOKENS,
            recent_scene=recent, world_context=cards_ctx,
        )
        dt = time.perf_counter() - t0
        afters.append({"idx": idx, "input": r["input"], "text": final,
                       "report": report, "elapsed_s": round(dt, 2)})
        b = before.get(idx, {})
        bstr = f"{b.get('verdict', '?')}/rw={b.get('prose_rewritten')}/{_rules(b) or ''}"
        astr = f"{report.get('verdict')}/rw={report.get('prose_rewritten')}/{_rules(report) or ''}"
        flag = ""
        if b.get("prose_rewritten") != report.get("prose_rewritten") or _rules(b) != _rules(report):
            flag = "  <-- CHANGED"
        if report.get("error"):
            astr += f"/err={report['error']}"
        print(f"{idx:<5}{bstr:<34}{astr:<34}{dt:>5.0f}s{flag}")
    with open(os.path.join(_DIR, corpus, "audited_after.jsonl"), "w", encoding="utf-8") as f:
        for a in afters:
            f.write(json.dumps({"corpus": corpus, **a}, ensure_ascii=False) + "\n")

    # Acceptance: the prod electricity case must keep the established ability.
    if corpus == "prod":
        raw16 = next((r for r in raws if r["idx"] == 16), None)
        aft16 = next((a for a in afters if a["idx"] == 16), None)
        if raw16 and aft16:
            def has_elec(t):
                tl = t.lower()
                return sum(tl.count(m) for m in ELECTRIC_MARKERS)
            print(f"\n[ACCEPTANCE prod idx16] electric markers: raw={has_elec(raw16['text'])} "
                  f"before_audited={has_elec(_load('prod','audited')[16]['text'])} "
                  f"after_audited={has_elec(aft16['text'])}  verdict={aft16['report'].get('verdict')}")


async def main() -> None:
    tone, cards_ctx = load_scenario_context()
    auditor = AuditorEngine(LLMRouter(LLMConfig()))
    which = os.environ.get("CORPUS", "both").lower()
    names = ["stress", "prod"] if which == "both" else [which]
    for corpus in names:
        await run_corpus(corpus, auditor, cards_ctx, tone)
    print("\nDONE — after reports in docs/fase3b_ab/{corpus}/audited_after.jsonl")


if __name__ == "__main__":
    asyncio.run(main())
