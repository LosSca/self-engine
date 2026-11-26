"""The intent advisor's learned vocabulary, as an editable table (/api/intent).

The advisor reads one sentence at a time and costs a model call; whatever it
learns is written down so the identical utterance is answered from this table
next time. This router is the read/write face of that table for the settings
panel: list, add by hand, correct, delete, and clear one world's vocabulary.

Rows never decide anything on their own. Each one only names an action plus the
words that pointed at its target, and the caller re-runs both through the
deterministic parser and the rules engine - so editing a row can at worst make
a sentence stop resolving, never make the rules accept something they forbid.

Scope is a world id: "推开门" means a different door on a station and in a
tavern, while every campaign played in one world shares what it taught.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db.intent_memory_store import IntentMemoryStore

router = APIRouter(tags=["intent-memory"])


def _store() -> IntentMemoryStore:
    return IntentMemoryStore()


class LearningIn(BaseModel):
    """One hand-written row. Fields mirror what the advisor records."""

    scope_key: str = Field(..., min_length=1, description="world id this reading belongs to")
    phrase: str = Field(..., min_length=1, description="what the player types, envelope optional")
    action: str = Field(..., min_length=1, description="engine action name")
    fragment: str = Field("", description="the words naming the target")
    lang: str = Field("", description="'zh' or empty for other languages")
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class LearningPatch(BaseModel):
    """Corrections: only the fields sent are changed."""

    action: Optional[str] = Field(None, min_length=1)
    fragment: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


@router.get("/intent")
def list_learnings(
    scope: Optional[str] = Query(None, description="world id; omit for every world"),
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    store = _store()
    try:
        rows = store.list(scope, limit=limit)
    finally:
        store.close()
    return {"learnings": rows, "count": len(rows)}


@router.post("/intent")
def add_learning(body: LearningIn) -> Dict[str, Any]:
    store = _store()
    try:
        row_id = store.remember(
            body.scope_key,
            body.phrase,
            body.action,
            body.fragment,
            lang=body.lang,
            confidence=body.confidence,
            source="manual",
        )
    finally:
        store.close()
    if row_id is None:
        raise HTTPException(status_code=400, detail="phrase and action must not be empty")
    return {"status": "ok", "id": row_id}


@router.patch("/intent/{row_id}")
def update_learning(row_id: int, body: LearningPatch) -> Dict[str, Any]:
    store = _store()
    try:
        rows = {row["id"]: row for row in store.list(None, limit=1000)}
        current = rows.get(int(row_id))
        if current is None:
            raise HTTPException(status_code=404, detail=f"no learning {row_id}")
        # remember() is an upsert on (scope, lang, phrase), so "edit" is a
        # rewrite of the same key: no second table and no lost history.
        store.remember(
            current["scope_key"],
            current["phrase"],
            body.action if body.action is not None else current["action"],
            body.fragment if body.fragment is not None else current["fragment"],
            lang=current["lang"],
            confidence=(
                body.confidence if body.confidence is not None else current["confidence"]
            ),
            source="manual",
        )
    finally:
        store.close()
    return {"status": "ok", "id": row_id}


@router.delete("/intent/{row_id}")
def delete_learning(row_id: int) -> Dict[str, Any]:
    store = _store()
    try:
        removed = store.delete(row_id)
    finally:
        store.close()
    if not removed:
        raise HTTPException(status_code=404, detail=f"no learning {row_id}")
    return {"status": "ok", "deleted": int(row_id)}


@router.delete("/intent")
def clear_learnings(scope: str = Query(..., min_length=1, description="world id to forget")) -> Dict[str, Any]:
    store = _store()
    try:
        removed = store.clear(scope)
    finally:
        store.close()
    return {"status": "ok", "removed": removed}
