"""Aggregate blind-judge rows by arm using the id->arm map. Reads the judge
output (rows JSON on argv[1]) and judge_map.json, prints the A/B table for the
LLM-judged tics alongside the regex sanity counts."""
import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DIR = os.path.join(_ROOT, "docs", "fase3a_ab")

rows = json.load(open(sys.argv[1], encoding="utf-8"))
if isinstance(rows, dict):
    rows = rows.get("rows", rows)
id_map = json.load(open(os.path.join(_DIR, "judge_map.json"), encoding="utf-8"))

agg = {"OLD": {}, "NEW": {}}
counts = {"OLD": 0, "NEW": 0}
metrics = ["confirmed_recaps", "npc_recaps", "rule_of_three", "fake_metrics", "em_dash_interruptive"]
for a in agg:
    agg[a] = {m: 0 for m in metrics}

for r in rows:
    arm = id_map[r["id"]]["arm"]
    counts[arm] += 1
    for m in metrics:
        agg[arm][m] += int(r.get(m, 0) or 0)

print(f"responses: OLD={counts['OLD']} NEW={counts['NEW']}\n")
hdr = f"{'tic':<22}{'OLD /resp':>12}{'NEW /resp':>12}{'Δ':>10}"
print(hdr); print("-" * len(hdr))
for m in metrics:
    o = agg["OLD"][m] / max(1, counts["OLD"])
    n = agg["NEW"][m] / max(1, counts["NEW"])
    delta = f"{(n-o)/o*100:+.0f}%" if o else ("0%" if n == 0 else "n/a")
    print(f"{m:<22}{o:>12.2f}{n:>12.2f}{delta:>10}")

out = {"counts": counts, "totals": agg,
       "per_resp": {a: {m: agg[a][m] / max(1, counts[a]) for m in metrics} for a in agg}}
json.dump(out, open(os.path.join(_DIR, "judged_summary.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("\nsaved -> docs/fase3a_ab/judged_summary.json")
