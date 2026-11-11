# Phase 7 — completeness review

This is an audit of the upgrade itself, written after Phase 6 shipped. It asks
one question of every promise in the guide: *what would prove this, and does
that proof exist in the repository?*

## Verdict table

| Guide promise | Evidence | Verdict |
|---|---|---|
| Rules decide facts, LLM only narrates | `ActionService.execute` calls `RulesEngine.evaluate` before any model; `test_architecture_acceptance.py::TestScenarioSixTheModelFails::test_a_plain_action_asks_no_model` asserts a `look` makes **zero** model calls | met |
| NPCs share one agent, hold their own memory | `npc_dossier.dossier_for` reads only that NPC's `NpcMind`; `TestScenarioFourKnowledgeIsolation` | met |
| Lore never lands in canon unverified | `world_evolution.PERSONAL_KINDS` are never eligible; `test_a_rumour_never_becomes_canon` | met |
| A rumour must not be reported as committed | `world_evolution.commit` only marks a claim when `_carried()` finds it in what was staged; `test_a_claim_the_world_already_has_is_never_called_committed` | met |
| Leaving the game freezes time | `Clock.advance` returns itself while paused; `GameState` has no wall-clock field to catch up from | met |
| Model names never live in code | `agents/*` read `AGENTS_<ROLE>_MODEL`; an unconfigured role reuses the main model | met for the agent lane |
| No new heavy infrastructure | no queue, no vector store, no graph dependency added; `evolution_proposals` is the retryable record | met |
| Migration without data loss | schema v2 → v4, additive only, `test_agent_calls.py` and `test_routes_evolution.py` cover the tables | met |

## Where the code does not yet match the guide

1. **Model names still live in the legacy lane.**
   `backend/app/api/routes_game.py:54` (`_OPENAI_MODEL = "gpt-5.6-sol"`),
   `backend/app/engines/llm_router.py:402,423` (context window and sampling lists),
   `backend/app/services/narrator_factory.py:165` and
   `backend/app/services/narrative_service.py:186` (provider fallback tables) all
   contain literal model ids. They are the *existing* defaults for the old chat
   lane, not the new agent lane — but the guide says names must not be hard-coded,
   and a user swapping providers should not have to edit source. This is the one
   clear remaining gap.
2. **Two write lanes.** `ActionService` is the authoritative entry point, but
   `game_session.py` (3370 lines) still calls models directly at lines 698,
   784, 975, 2399 and 3219 and still spawns a prose-derived world tick. It is
   paused with the clock, never removed.
3. **No UI for evolution.** Proposals are API-only; the debug panel shows model
   calls, not world proposals.
4. **Frontend never passed `vite build`.** The DSH preview hook breaks
   `vite.config.js` inside this sandbox, so the frontend is verified only by an
   esbuild parse and a source-wiring script.

## What this review changed

- `world_evolution.commit` now takes the list of staged ops and only reports a
  claim as committed when its name actually reached the draft; the previous
  version counted every pending canonical claim.
- The same honesty rule is applied in `run()`: an eligible claim the world
  refused to stage is moved to `rejected` with the reason, instead of silently
  staying `pending` while `committed` claimed success.
- Regression test `test_a_claim_the_world_already_has_is_never_called_committed`.

## Numbers

- Before the review: 1623 passed, 8 skipped.
- After the review: **1625 passed, 8 skipped** (full suite, `-q -p no:cacheprovider`).
- Commits: Phase 0–6 in `7b7b4df5 … c9ff11c8`; this review is the commit after it.
