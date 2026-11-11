# Phase 6 — integration review

This is the last phase: what the upgrade changed, what it deliberately did not
change, and how to check it yourself.

## The six scenarios, as tests

`backend/tests/services/test_architecture_acceptance.py` drives the real entry
points against a real SQLite store. Each class is one scenario from the guide.

| Scenario | Test | What it proves |
|---|---|---|
| 1. NPC dialogue | `TestScenarioOneNpcDialogue` | The narrator sees the NPC file (name, personality, mood, secret); the rules engine still decides that the talk happened, and it still commits. |
| 2. Player worldbuilding | `TestScenarioTwoPlayerWorldbuilding` | A mentioned monastery becomes a proposal that passes `validate_references` before it is written; without `commit` nothing is written at all. |
| 3. World grows the story | `TestScenarioThreeWorldGrowsTheStory` | A rumour never lands in canon; a claim about a place the world already has is rejected with a readable reason. |
| 4. Knowledge isolation | `TestScenarioFourKnowledgeIsolation` | Two NPCs share the engine; the priest's secret never reaches the merchant's dossier. |
| 5. Leaving the game | `TestScenarioFiveLeavingTheGame` | A paused campaign spends no time; resuming does not catch up. |
| 6. Model failure | `TestScenarioSixTheModelFails` | A dead narrator leaves the committed turn intact (`status="failed"`, prose fallback); the System Agent degrades instead of raising; a plain action calls no model at all. |

## What is new (by phase)

| Phase | Module | Entry point |
|---|---|---|
| 1 | `app/engines/agent_router.py` | `AgentRuntime.ask(role, ...)`, `router_for(role)`; roles configured by `AGENTS_<ROLE>_*` env vars, never hard-coded model names. |
| 1 | `app/engines/system_agent.py` | `SystemAgent.classify`, `review_ops`; deterministic first, model only for long unverified claims. |
| 2 | `app/engines/npc_dossier.py` | `dossier_for(world, state, entity_id, mind=...)` — NPCs are data assembled per turn. |
| 2 | `app/engines/player_context.py` | `PlayerContextBuilder.build(...)` — layered context with `fact/canon/belief/guess/history` labels. |
| 3 | `app/services/world_evolution.py` | `WorldEvolutionService.run/propose/commit_pending`; proposals and executions live in `evolution_proposals`. |
| 4 | `app/engines/memory_engine.py` | Provenance on every crystal (`source`, `observer`, `confidence`, `witnessed_names`); `provenance_label()` in the narration prompt. |
| 5 | `app/engines/game_clock.py` | `Clock`, `duration_minutes`, `open_now`; advanced inside `ActionService._apply`, frozen while paused. |

## API surface added

```
GET  /api/v1/agents
GET  /api/v1/campaigns/{id}/agent-calls
POST /api/v1/worlds/{id}/evolution/propose
GET  /api/v1/worlds/{id}/evolution/proposals
POST /api/v1/worlds/{id}/evolution/proposals/{pid}/approve
POST /api/v1/worlds/{id}/evolution/proposals/{pid}/reject
POST /api/game/{id}/pause
POST /api/game/{id}/resume
GET  /api/game/{id}/clock
```

Nothing existing changed shape; the only additions to existing payloads are
`minutes_spent` and `clock` on `ActionResult`, and `day`/`minute` on `GameState`
(both defaulted, so old saves and old clients keep working).

## Database

Schema version 2 → 4. Both steps are additive and run automatically on open:

- v3: `agent_calls` (role, task, provider, model, status, degraded, tokens, elapsed_ms, error) + index.
- v4: `evolution_proposals` (kind, status, confidence, payload/evidence/warnings/result, timestamps) + two indexes.

No column was dropped or rewritten, and no historical row was touched. A
reader that only wants to read opens with `GameStateStore(path, migrate=False)`
so inspecting a database never rewrites it.

## What was deliberately not built

- An agent per NPC, NPC-to-NPC chatter, or offline world simulation. The clock
  proves it: there is no wall-clock timestamp to catch up from.
- A task queue. Evolution proposals are recorded synchronously with the
  request that produced them; the table *is* the retryable record, and
  `commit_pending` is the retry.
- Vector search or a graph store for memory. Retrieval is the existing keyword
  scoring plus per-NPC witness filtering.
- Any model name in code. `AGENTS_SYSTEM_MODEL` / `AGENTS_NARRATIVE_MODEL` /
  `AGENTS_MEMORY_MODEL` decide, and an unconfigured role inherits the main
  model so a fresh checkout behaves exactly as before.

## Known gaps

- The legacy chat lane (`routes_game` + `GameSession`) still has its own
  narrative pipeline and its own prose-derived `narrative_time_seconds` world
  tick. It is paused/resumed with the clock, but it was not migrated onto
  `ActionService`; that migration is the next obvious piece of work.
- Storyline growth is still one-shot (`/scenarios/author`), not part of the
  evolution lane; `world_evolution` can carry a `storyline` proposal, but
  nothing consumes it yet.
- The frontend has no UI for evolution proposals: they are API-only, visible
  through the debug endpoints. The model-call panel (`DevtoolsPanel` → "模型调用")
  is the only new UI, and it was verified by a source-wiring script, not a
  browser test.

## Verify locally

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

Then, with the server running, `GET /api/v1/agents` shows each role and whether
it is configured — it never shows a key. `GET /api/game/{campaign}/clock`
shows the in-game clock and whether it is frozen.
