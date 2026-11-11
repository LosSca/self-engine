"""Anonymize A/B prose for a blind LLM-judge pass: interleave OLD/NEW, assign
opaque ids, write one small text file per response (judge never sees the arm),
and persist the id->arm map for post-hoc aggregation."""
import json
import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DIR = os.path.join(_ROOT, "docs", "fase3a_ab")
_RESP = os.path.join(_DIR, "resp")


def load(arm):
    out = []
    with open(os.path.join(_DIR, f"{arm}.jsonl"), encoding="utf-8") as f:
        for line in f:
            out.append(json.loads(line))
    return out


def main():
    os.makedirs(_RESP, exist_ok=True)
    new, old = load("new"), load("old")
    interleaved = []
    for i in range(max(len(new), len(old))):
        if i < len(new):
            interleaved.append(("NEW", new[i]))
        if i < len(old):
            interleaved.append(("OLD", old[i]))
    id_map, ids = {}, []
    for k, (arm, rec) in enumerate(interleaved):
        rid = f"r{k:02d}"
        ids.append(rid)
        id_map[rid] = {"arm": arm, "idx": rec["idx"]}
        with open(os.path.join(_RESP, f"{rid}.txt"), "w", encoding="utf-8") as f:
            f.write(rec["text"])
    with open(os.path.join(_DIR, "judge_map.json"), "w", encoding="utf-8") as f:
        json.dump(id_map, f, ensure_ascii=False, indent=2)
    print(json.dumps(ids))


if __name__ == "__main__":
    main()
