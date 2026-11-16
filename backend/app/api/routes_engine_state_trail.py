"""`GET .../event-executions`: the one campaign view registered right after the action routes.

The handler and its model are unchanged; it lives here so the facade can place
it at the exact position the single module had it.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from app.api.routes_engine_state import EventExecutionView, _get_store


router_event_executions = APIRouter()


@router_event_executions.get(
    "/campaigns/{campaign_id}/event-executions",
    response_model=List[EventExecutionView],
)
def list_event_executions(
    campaign_id: str,
    limit: int = Query(100, ge=1, le=500),
    status: Optional[str] = Query(None),
    after_version: int = Query(0, ge=0),
):
    """Event execution history, newest first.

    Derived from the committed `event.fired`/`event.failed`/`event.skipped`
    domain events, so it can only ever show what actually happened.
    """
    wanted = (status or "").strip().lower()
    store = _get_store()
    try:
        rows = store.list_domain_events(
            campaign_id,
            event_type_prefix="event.",
            limit=500 if wanted else limit,
            after_version=after_version,
        )
    finally:
        store.close()

    executions: List[Dict[str, Any]] = []
    for row in rows:
        outcome = row["event_type"].split(".", 1)[1] if "." in row["event_type"] else ""
        if wanted and outcome != wanted:
            continue
        payload = row.get("payload") or {}
        executions.append(
            {
                "event_id": payload.get("event_id", ""),
                "name": payload.get("name", ""),
                "status": outcome,
                "reason": payload.get("reason", ""),
                "effects": payload.get("effects", []),
                "branch": payload.get("branch", ""),
                "parent_event_id": payload.get("parent_event_id", ""),
                "depth": payload.get("depth", 0),
                "state_version": row["state_version"],
                "request_id": row["request_id"],
                "created_at": row["created_at"],
            }
        )
        if len(executions) >= limit:
            break
    return executions
