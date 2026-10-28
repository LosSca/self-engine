"""Anonymize the FASE 3b A/B corpora for a blind LLM-judge pass.

Emits two products:
  docs/fase3b_ab/judge_input.json  -> given to the blind judge (no arm labels):
     tic_rows: every raw+audited response, opaque ids, interleaved. Judge counts tics.
     pairs:    only pairs the Auditor REWROTE, as version_a/version_b (arm hidden,
               assignment alternates). Judge checks meaning preservation + which is
               cleaner; agency pairs carry the claimed rule for verification.
  docs/fase3b_ab/judge_map.json    -> kept back for aggregation (id -> corpus/arm/idx).
"""
import json
import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DIR = os.path.join(_ROOT, "docs", "fase3b_ab")


def _load(corpus: str, arm: str) -> list[dict]:
    path = os.path.join(_DIR, corpus, f"{arm}.jsonl")
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def main() -> None:
    corpora = [c for c in ("stress", "prod") if os.path.isdir(os.path.join(_DIR, c))]
    tic_rows, tic_map = [], {}
    pairs, pair_map = [], {}

    # tic rows: interleave raw/audited across corpora, opaque sequential ids
    bundle = []
    for corpus in corpora:
        raw = {r["idx"]: r for r in _load(corpus, "raw")}
        aud = {a["idx"]: a for a in _load(corpus, "audited")}
        for idx in sorted(raw):
            bundle.append((corpus, "raw", idx, raw[idx]["text"]))
            if idx in aud:
                bundle.append((corpus, "audited", idx, aud[idx]["text"]))

    for k, (corpus, arm, idx, text) in enumerate(bundle):
        rid = f"t{k:03d}"
        tic_rows.append({"id": rid, "text": text})
        tic_map[rid] = {"corpus": corpus, "arm": arm, "idx": idx}

    # pairs: only where the Auditor rewrote; alternate version_a assignment by counter
    pc = 0
    verify_files = []
    for corpus in corpora:
        raw = {r["idx"]: r for r in _load(corpus, "raw")}
        for a in _load(corpus, "audited"):
            rep = a.get("report", {})
            if not rep.get("prose_rewritten"):
                continue
            idx = a["idx"]
            raw_text = raw[idx]["text"]
            aud_text = a["text"]
            pid = f"p{pc:02d}"
            raw_is_a = (pc % 2 == 0)
            version_a = raw_text if raw_is_a else aud_text
            version_b = aud_text if raw_is_a else raw_text
            claimed = [c.get("rule_violated") for c in (rep.get("corrections") or [])]
            # prior context = up to 2 preceding turns (same for both versions; the
            # judge needs it to catch context-dependent excisions like an ability the
            # player declared a turn earlier).
            prior = []
            for j in (idx - 2, idx - 1):
                if j in raw:
                    prior.append(f"PLAYER: {raw[j]['input']}\nNARRATOR: {raw[j]['text']}")
            prior_context = "\n\n".join(prior) or "(this is the opening turn; no prior context)"
            pairs.append({
                "id": pid, "player_input": a["input"],
                "version_a": version_a, "version_b": version_b, "claimed_fixes": claimed,
            })
            pair_map[pid] = {
                "corpus": corpus, "idx": idx,
                "a_arm": "raw" if raw_is_a else "audited",
                "b_arm": "audited" if raw_is_a else "raw",
                "claimed_fixes": claimed,
            }
            verify_files.append((pid, a["input"], version_a, version_b, prior_context))
            pc += 1

    with open(os.path.join(_DIR, "judge_input.json"), "w", encoding="utf-8") as f:
        json.dump({"tic_rows": tic_rows, "pairs": pairs}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(_DIR, "judge_map.json"), "w", encoding="utf-8") as f:
        json.dump({"tic_rows": tic_map, "pairs": pair_map}, f, ensure_ascii=False, indent=2)

    # Anonymized files for the blind judge (opaque ids only; no arm ever on disk).
    resp_dir = os.path.join(_DIR, "resp")
    os.makedirs(resp_dir, exist_ok=True)
    for row in tic_rows:
        with open(os.path.join(resp_dir, f"{row['id']}.txt"), "w", encoding="utf-8") as f:
            f.write(row["text"])
    for pid, pin, va, vb, ctx in verify_files:
        vdir = os.path.join(_DIR, "verify", pid)
        os.makedirs(vdir, exist_ok=True)
        for fname, content in (
            ("player_input.txt", pin), ("prior_context.txt", ctx),
            ("version_a.txt", va), ("version_b.txt", vb),
        ):
            with open(os.path.join(vdir, fname), "w", encoding="utf-8") as f:
                f.write(content)

    print(f"tic_rows={len(tic_rows)} (raw+audited, {len(corpora)} corpora), "
          f"rewritten_pairs={len(pairs)}; wrote resp/*.txt and verify/*/")


if __name__ == "__main__":
    main()
