# Phase 4 — Memory provenance

The four-tier crystal pyramid (SHORT → MEDIUM → LONG → MEMORY) is unchanged.
What was missing was a record of *why* a memory exists and *how far* to trust
it, so a paraphrased summary could be read exactly like a witnessed fact.

## Fields added to `MemoryCrystal`

| Field | Meaning |
|---|---|
| `memory_id` | `mem_` + sha1 of (campaign, tier, span)[:16]. Stable, so a rebuild produces the same id. |
| `source` | `events` (crystallised from raw events), `consolidation` (merged from lower tiers), or empty for crystals written before this field existed. |
| `observer` | `player` for the shared record, otherwise the character the record belongs to. |
| `confidence` | 0..1. Tier defaults: SHORT 1.0, MEDIUM 0.9, LONG 0.8, MEMORY 0.7. A merge steps down 0.02 from the best inherited value: it is a paraphrase of paraphrases. |
| `importance` | 0..1 salience, inherited as the max of the merged sources. |
| `visibility` | `npc` when the crystal has a witness audience, `player` when nobody witnessed it. |
| `related_entities` | Proper names from the source events, most frequent first, at most 12. A merge unions its sources in order. |
| `status` | `committed` for anything that reached a tier. |
| `witnessed_names` | Display spelling of the audience (`witnessed_by` is case-folded for comparison, so it cannot be shown as written). |

All of it is persisted in the existing `MEMORY_CRYSTAL` event payload, and
`GameSession._rebuild_memory_crystals` restores it. Crystals from before this
change carry no provenance fields and are rebuilt with a derived `memory_id`
and their tier's default confidence — nothing is invented about their source.

## How the narrator sees it

`MemoryEngine.render_permanent_context` and `provenance_label()` prefix a
MEMORY-tier crystal with one line:

```
(consolidation; witnessed by Aldo; paraphrased)
Vault exists, and the monks kept a second key.
```

Below 0.5 confidence the label says `uncertain - a lead, not a fact`. The text
is still rendered: a low-confidence memory is a hint the narrator may use, and
dropping it would be a worse failure than labelling it.

## What was deliberately not added

- No vector store, no embedding index, no graph. Retrieval is still the existing
  keyword scoring plus the per-NPC witness filter.
- No `candidate` pipeline. A crystal becomes visible only once a tier is
  committed; there is no half-written memory state to clean up.
- No retroactive rewrite of old crystals. Their provenance stays unknown,
  which is itself information.

## Tests

`backend/tests/engines/test_memory_provenance.py` — 9 cases:

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/engines/test_memory_provenance.py
```

A witnessed crystal records source, observer, entities and confidence; the label
names the witness; an unwitnessed crystal is a player account; consolidation
keeps provenance and lowers confidence; provenance survives persistence; legacy
crystals render with no label; ids are stable per span and differ per campaign;
the permanent context block carries the label.
