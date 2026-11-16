"""FASE 3b end-to-end check: run the real post-hoc auditor over crafted prose.

Exercises AuditorEngine.audit against a live provider (DeepSeek by default) with
scenario-agnostic prose that carries known defects, and confirms: clean prose passes
untouched, an agency violation is corrected, and a rewrite preserves [ITEM_*] tags.

Run from backend/:  python -m scripts.validate_fase3b_auditor
"""
from __future__ import annotations
import asyncio
import os
import re
import sys
from pathlib import Path


def _load_env() -> None:
    root = Path(__file__).resolve().parents[2]
    for p in (root / ".env", root / "backend" / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip()


_load_env()

from app.engines.llm_router import LLMRouter, LLMConfig, LLMProvider
from app.engines.auditor_engine import AuditorEngine, _ITEM_TAG_RE

CASES = [
    {
        "name": "clean prose (expect: clean, untouched)",
        "player_input": "[DO] I step into the hall.",
        "prose": (
            "You step into the hall. Dust drifts through the light falling from the high "
            "windows. A guard looks up from his ledger, sets down his pen, and waits for you "
            "to speak."
        ),
    },
    {
        "name": "agency violation (expect: corrected, player's invented speech/decision excised)",
        "player_input": "[DO] I walk to the door.",
        "prose": (
            "You walk to the door. \"I'll take the eastern road at dawn,\" you say, and you "
            "decide to abandon the city for good, certain now that nothing here can hold you."
        ),
    },
    {
        "name": "item tag + tic (expect: tag preserved verbatim under guard)",
        "player_input": "[DO] I lift the sword from the altar.",
        "prose": (
            "You lift the blade from the altar. [ITEM_ADD:Silver Sword|weapon|taken from the "
            "altar] It is not heavy, but light, lighter than any steel you have ever held."
        ),
    },
]


async def main() -> int:
    provider = os.environ.get("AUDIT_TEST_PROVIDER", "deepseek")
    model = os.environ.get("AUDIT_TEST_MODEL", "deepseek-v4-flash")
    cfg = LLMConfig(
        primary_provider=LLMProvider(provider),
        primary_model=model,
        temperature=0.2,
    )
    auditor = AuditorEngine(LLMRouter(cfg))

    ok = True
    for c in CASES:
        print("\n" + "=" * 72)
        print(c["name"])
        orig_tags = sorted(_ITEM_TAG_RE.findall(c["prose"]))
        final, report = await auditor.audit(
            prose=c["prose"],
            player_input=c["player_input"],
            language="en",
            tone_instructions="Grounded, sensory second-person narration.",
            max_tokens=800,
        )
        print("verdict:", report.get("verdict"), "| rewritten:", report.get("prose_rewritten"))
        for corr in report.get("corrections", []) or []:
            print("  -", corr.get("rule_violated"), "::", corr.get("reasoning", "")[:160])
        if report.get("marker_guard_rejected"):
            print("  [marker guard REJECTED the rewrite -> original preserved]")
        print("--- final prose ---")
        print(final)
        # Invariant: item tags never lost, regardless of verdict.
        final_tags = sorted(_ITEM_TAG_RE.findall(final))
        if final_tags != orig_tags:
            print(f"  !! TAG INVARIANT VIOLATED: {orig_tags} -> {final_tags}")
            ok = False
        elif orig_tags:
            print(f"  tag invariant OK: {orig_tags} preserved")

    print("\n" + "=" * 72)
    print("RESULT:", "PASS" if ok else "FAIL (tag invariant broken)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
