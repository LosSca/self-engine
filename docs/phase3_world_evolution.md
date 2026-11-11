# Phase 3 — World Evolution

What a player says in a turn can now become part of the world, but only
through a proposal that is validated and recorded. Nothing about that path
runs a model: classification is deterministic, validation reuses the
authoring validator, and the audit row says who proposed what and why it was
accepted or refused.

## The pipeline

```
player text / explicit statements
        |  system_agent.classify_locally      (rules, no model)
        v
  statements: kind + confidence + entities
        |  statements_to_proposals           (entity resolution)
        v
  EvolutionProposal per claim
        |  _canon_conflict                   (conflict detection)
        v
  pending / rejected
        |  _stage -> authoring_session.stage_ops    (validation on a deep copy)
        v
  validated draft  ->  world_service.save_world   (commit, only if asked)
```

Two separate decisions are made, and they are not the same thing:

- **Is this canonical at all?** `is_canonical(kind)` is true for locations,
  items, characters, events, quests and `world_canon`. A rumour, a belief, a
  secret or a player backstory is never canonical, so it can never reach the
  world document through this path no matter how confident the model was.
- **Does it get committed now?** only when `commit=true` and the confidence
  reaches `auto_commit`. A human approving a pending proposal sets the
  threshold to zero: the reviewer already made that call, and the model's
  hesitation is not evidence about the world.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/worlds/{world_id}/evolution/propose` | draft (default) or commit one turn's claims |
| GET | `/api/v1/worlds/{world_id}/evolution/proposals?status=&kind=&limit=` | the audit trail |
| POST | `/api/v1/worlds/{world_id}/evolution/proposals/{proposal_id}/approve` | commit one pending proposal |
| POST | `/api/v1/worlds/{world_id}/evolution/proposals/{proposal_id}/reject?reason=` | refuse it, keeping the reason |

`POST .../propose` takes either free `text` (classified deterministically) or
explicit `statements` (`text`, `kind`, `confidence`, `entities`). Explicit
statements win, which is what an editor sends when the author already said
what they meant.

## Storage

Migration 4 of `GameStateStore` adds `evolution_proposals`:

```
id, world_id, campaign_id, kind, status, origin, statement, confidence,
source, observer, payload_json, evidence_json, warnings_json, error,
result_json, created_at, updated_at
```

`record_proposal` merges instead of replacing, because a status change (or a
rejection with a reason) carries only the field it changed. A dict decoded from
the table overwrites with its decoded values; a dataclass overwrites only the
fields the caller actually set. A failed audit write is logged, never raised:
losing an audit row must not lose a turn.

## What the decisions look like

`test_a_redeclared_place_is_rejected_with_a_reason` — restating a place the
world already has is refused, with the text the rule produced:

```
the world already defines location 'old chapel'; edit it instead of re-declaring it
```

`test_a_rumour_never_becomes_canon` — "I heard the monks kept a locked vault
in the cellar." classifies as a rumour, stays pending even with
`auto_commit=0.0`, and never reaches the world document.

`test_propose_is_a_dry_run_by_default` — a proposal without `commit=true`
leaves the stored world byte-identical.

## Storage hygiene this exposed

Two behaviours were making the test suite write into the checkout instead of
a temporary directory, and both are now fixed because they are wrong beyond
the tests:

- `GameStateStore` ran its migrations from the constructor, so merely *opening*
  a database rewrote it. A read-only caller (the knowledge route that maps a
  scenario to its events) could therefore fail with `attempt to write a
  readonly database`. The constructor now takes `migrate=False` and that
  caller uses it. Opening a database no longer changes it.
- `routes_game` opens its event and trace stores at import time, so a module
  imported before a test patched the paths kept pointing at the real file even
  after `importlib.reload(app.main)` (reloading `app.main` does not rebuild
  `app.api.routes_game`). Test fixtures now patch every store path — including
  `LLM_TRACE_DB_PATH` — before importing, test modules no longer import
  `app.main` at module scope, and the one reload-based fixture drops
  `app.api.routes_game` from `sys.modules` first.

`_context_window_quietly` also exists now: memory context is enrichment, and a
store that cannot be read should not turn a timeskip into a 500.


## Deliberate omissions

- No background job runs this. It fires on an explicit request only, which is
  what keeps the engine player-centric: nothing evolves while nobody plays.
- No model call happens on the write path. `WorldEvolutionService.review`
  exists for a caller that wants a structured second opinion on a pending
  proposal, and it is not wired into anything automatic.
- Campaign state is never touched here. NPC beliefs and rumours belong to the
  character, not the world; the only writer of campaign state is
  `ActionService`.

## Tests

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/api/test_routes_evolution.py
```

Ten cases: dry run, commit, redeclaration refused, rumour never canonical,
unknown world 404, approve, double approve 409, reject with reason, list
filtering, and the contract check that every route answers in a typed model.