# Phase 5 — GameClock: the world only moves when the player does

## The rule, as code

`GameState` carries `day` (default 1) and `minute` of day (default 480, i.e.
08:00). That is the entire clock. There is no wall-clock comparison, no
background job and no persisted timestamp that a reopening could subtract
from: **offline catch-up is impossible by construction**, because nothing
stores when the player left.

`app/engines/game_clock.py`:

| Function | Purpose |
|---|---|
| `Clock.from_state(state)` | Read the clock; saves written before these fields existed read as Day 1, 08:00. |
| `Clock.advance(minutes)` | Returns a new clock. A paused clock returns itself, so a forgotten resume cannot leak time. |
| `duration_minutes(action, params)` | Explicit param → duration text → verb default → unknown-verb default. |
| `parse_duration(text)` | A bounded vocabulary ("2 hours", "30 min", "2 days"). Not a natural-language parser: the clock has to stay testable. |
| `is_open_now` / `active_shift` / `open_now` | Shop hours, including windows that cross midnight. |
| `phase_text` | "dawn" / "evening" for prompts, localised. |

## Where time is spent

The action catalogue declares each verb's cost (`ActionSpec.minutes`): looking
and talking 0, moving 10, taking/opening 1, investigating 5. A verb that
declares nothing falls back to `DEFAULT_ACTION_MINUTES`; a scenario-authored
verb nobody registered costs `UNKNOWN_ACTION_MINUTES` (1) rather than nothing.

`ActionService._apply` advances the clock just before the commit, in the same
transaction that writes the state, the request row and the domain events. So:

- a rejected action spends no time (it never reaches `_apply`);
- a duplicate request replays the recorded result and spends no time;
- the returned `ActionResult.minutes_spent` and `.clock` describe the commit;
- narration happens after the commit and can never change the clock.

The clock is advanced through `advance_state`, which consults `clock_paused`.
`ActionService` takes that as an injected `Callable[[campaign_id], bool]` so the
domain layer does not import the API layer: `routes_game.campaign_clock_paused`
answers "is a player currently in this campaign?" — **a campaign with no cached
session counts as paused**, which is the Player-Centric rule stated as one
predicate. A probe that raises is treated as "not paused" (the turn is alive,
so time may pass), and the failure is swallowed rather than failing the turn.

## Merchant hours instead of NPC simulation

`buy` now refuses with `shop_closed` when the merchant declares
`attributes["hours"] = [{"opens_minute": 540, "closes_minute": 720}]` and the
clock is outside it. No hours declared means always open, so every existing
world behaves exactly as before. This is the whole model of "NPC daily
behaviour": the shop is open because the clock says so, not because something
simulated the shopkeeper.

## Leaving and coming back

`POST /api/game/{campaign_id}/pause` cancels the legacy session's pending world
ticks and refuses further turns; `resume` lifts both; `GET /clock` now returns
the clock label, the phase and whether it is frozen:

```json
{"state": "running", "clock_paused": false,
 "clock": {"day": 1, "minute": 490, "label": "Day 1, 08:10",
           "time_of_day": "morning", "paused": false, "abs_minute": 490}}
```

## Tests

`backend/tests/engines/test_game_clock.py` (17 cases) and
`backend/tests/services/test_game_clock_pause.py` (8 cases):

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/engines/test_game_clock.py tests/services/test_game_clock_pause.py
```

They cover midnight rollover, paused clocks never moving, old saves reading as
the default morning, verb costs, explicit params beating the table, a paused
campaign spending no time, a broken probe not freezing the world, rejected and
duplicate requests not buying time, closed shops, and the pause/resume route
pair with a live event loop proving background ticks stop.

## Not done here

- No day/night lighting, weather or seasonal effects: the clock is a number
  the rules may read, not a simulation.
- No time-driven event triggers yet. `GameEventDef` conditions do not include
  "hour is between X and Y"; that belongs to the event work, not the clock.
- The legacy chat lane still estimates `narrative_time_seconds` from prose for
  its world tick. That lane is sunsetting; the authoritative clock never
  reads it.
