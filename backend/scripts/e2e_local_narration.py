"""End-to-end check: real narration from the local model, real commits.

Runs the engine lane against temporary databases with `ENGINE_NARRATOR=auto`,
so the narrator is whichever model is configured (the local NInfer server when
`LOCAL_LLM_*` is set). It plays the demo village: look, take the iron key, open
the locked door, and then reloads the state to prove it survived.

    .venv\\Scripts\\python.exe scripts\\e2e_local_narration.py

Exit code 0 means: every turn committed, every narrative came back `ok` from the
model, and the reloaded state matches. Any other outcome prints why.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TMP = tempfile.mkdtemp(prefix="e2e_local_")
os.environ["GAME_STATE_DB_PATH"] = os.path.join(TMP, "game.db")
os.environ["SCENARIO_DB_PATH"] = os.path.join(TMP, "scenarios.db")
os.environ["EVENT_DB_PATH"] = os.path.join(TMP, "events.db")
os.environ.setdefault("ENGINE_NARRATOR", "auto")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import narrator_factory  # noqa: E402

WORLD = json.loads((BACKEND / "tests" / "fixtures" / "village_world.json").read_text(encoding="utf-8"))
WORLD["world_id"] = "e2e_local_world"
WORLD["scenario_id"] = "e2e_local"


def main() -> int:
    info = narrator_factory.describe()
    print("narrator:", json.dumps(info, ensure_ascii=False))
    if info["choice"] != "llm":
        print("REFUSE: no model configured; set LOCAL_LLM_BASE_URL and LOCAL_LLM_API_KEY")
        return 2

    failures = []
    with TestClient(app) as client:
        r = client.post("/api/v1/worlds", json=WORLD)
        print("world:", r.status_code)
        if r.status_code != 200:
            print(r.text[:400])
            return 2
        r = client.post("/api/v1/campaigns/e2e-camp/world", json={"world_id": "e2e_local_world"})
        print("bind:", r.status_code)
        if r.status_code != 200:
            print(r.text[:400])
            return 2

        for index, (utterance, request_id) in enumerate(
            [
                ("look around the village square", "e2e-1"),
                ("pick up the iron key", "e2e-2"),
                ("open the warehouse door", "e2e-3"),
            ],
            start=1,
        ):
            started = time.time()
            r = client.post(
                "/api/v1/campaigns/e2e-camp/text-actions",
                json={"text": utterance, "request_id": request_id, "narrate": True},
            )
            elapsed = time.time() - started
            if r.status_code != 200:
                failures.append(f"{request_id}: HTTP {r.status_code} {r.text[:200]}")
                continue
            body = r.json()
            result = body.get("result") or {}
            narrative = result.get("narrative") or {}
            print(
                f"\n[{index}] {utterance!r} -> {body.get('status')} "
                f"({elapsed:.1f}s, state v{result.get('state_version')})"
            )
            print("  parsed:", json.dumps(body.get("parsed"), ensure_ascii=False))
            if narrative.get("status") != "ok" or not narrative.get("text"):
                failures.append(f"{request_id}: narrative {narrative.get('status')} {narrative.get('error')}")
                print("  narrative FAILED:", narrative.get("error"))
            else:
                print("  narrative:", narrative["text"][:280].replace("\n", " "))

        state = client.get("/api/v1/campaigns/e2e-camp/state").json()
        diagnostics = client.get("/api/v1/campaigns/e2e-camp/diagnostics").json()
        print("\nstate:", json.dumps(
            {
                "version": state.get("version"),
                "location": state.get("location_id"),
                "doors": state.get("doors"),
                "inventory": state.get("inventory"),
                "quests": {k: v.get("status") for k, v in (state.get("quests") or {}).items()},
            },
            ensure_ascii=False,
        ))
        print("diagnostics ok:", diagnostics.get("ok"), "errors:", len(diagnostics.get("errors") or []))

        if state.get("doors", {}).get("warehouse_door") is not True:
            failures.append("door did not open")
        if (state.get("inventory") or {}).get("iron_key") != 1:
            failures.append("iron key not carried")
        if not diagnostics.get("ok"):
            failures.append("diagnostics reported problems")

    print("\nRESULT:", "OK" if not failures else "FAILED")
    for failure in failures:
        print(" -", failure)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
