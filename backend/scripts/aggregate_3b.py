"""Aggregate the FASE 3b blind-judge output against judge_map.json.

Usage: python scripts/aggregate_3b.py <judge_output.json>
  <judge_output.json> = the workflow's returned {census:[...], verify:[...]} object.

Prints the semantic-tic A/B table (raw vs audited, per corpus) and the per-pair
rewrite verdicts translated from version_a/version_b back into raw/audited arms.
"""
import json
import os
import sys
from collections import Counter, defaultdict

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DIR = os.path.join(_ROOT, "docs", "fase3b_ab")

TICS = ["npc_action_recap", "mechanical_triple", "gesture_gloss",
        "contrast_by_negation", "aphorism_or_oracle_closer", "pseudo_metric",
        "em_dash_interruptive"]


def main() -> None:
    jm = json.load(open(os.path.join(_DIR, "judge_map.json"), encoding="utf-8"))
    out = json.load(open(sys.argv[1], encoding="utf-8"))
    census = out.get("census", [])
    verify = out.get("verify", [])

    # ── census: aggregate by (corpus, arm) ──
    agg = defaultdict(lambda: defaultdict(int))
    counts = defaultdict(int)
    seen = set()
    for row in census:
        rid = row.get("id")
        m = jm["tic_rows"].get(rid)
        if not m or rid in seen:
            continue
        seen.add(rid)
        key = (m["corpus"], m["arm"])
        counts[key] += 1
        for t in TICS:
            agg[key][t] += int(row.get(t, 0) or 0)

    print(f"census rows scored: {len(seen)}/{len(jm['tic_rows'])}\n")
    for corpus in ("stress", "prod"):
        rk, ak = (corpus, "raw"), (corpus, "audited")
        nr, na = counts[rk], counts[ak]
        if not nr and not na:
            continue
        print(f"=== {corpus}: semantic tics per response (raw n={nr} -> audited n={na}) ===")
        hdr = f"{'tic':<28}{'raw':>8}{'audited':>10}{'delta':>9}"
        print(hdr)
        print("-" * len(hdr))
        for t in TICS:
            ro = agg[rk][t] / max(1, nr)
            ao = agg[ak][t] / max(1, na)
            delta = f"{(ao - ro) / ro * 100:+.0f}%" if ro else ("0%" if ao == 0 else "n/a")
            print(f"{t:<28}{ro:>8.2f}{ao:>10.2f}{delta:>9}")
        rtot = sum(agg[rk][t] for t in TICS)
        atot = sum(agg[ak][t] for t in TICS)
        print(f"{'TOTAL /resp':<28}{rtot / max(1, nr):>8.2f}{atot / max(1, na):>10.2f}")
        print()

    # ── verify: translate version_a/version_b votes into raw/audited arms ──
    def arm(pid, val):
        pm = jm["pairs"][pid]
        if val == "version_a":
            return pm["a_arm"]
        if val == "version_b":
            return pm["b_arm"]
        return val  # neither / equal / yes / mostly / no

    by_pair = defaultdict(list)
    for v in verify:
        by_pair[v["pid"]].append(v)

    print("=== rewrite verification (votes across judges, mapped to arm) ===")
    for pid in sorted(by_pair):
        pm = jm["pairs"][pid]
        judges = by_pair[pid]
        print(f"\n[{pid}] corpus={pm['corpus']} idx={pm['idx']} "
              f"claimed_fixes={pm['claimed_fixes']}  (a={pm['a_arm']}, b={pm['b_arm']}, {len(judges)} judges)")
        fields = ["same_events", "content_lost_in", "content_invented_in",
                  "established_ability_removed_in", "cleaner_prose", "overall_better"]
        for f in fields:
            tally = Counter(arm(pid, jv["verdict"].get(f)) for jv in judges)
            print(f"  {f:<32}{dict(tally)}")
        for jv in judges:
            r = jv["verdict"].get("reasoning", "")
            print(f"    - j{jv['judge']}: {r[:240]}")


if __name__ == "__main__":
    main()
