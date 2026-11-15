"""Memory fact API: what the world knows, and how it came to know it (/api/memory).

Reads are cheap and side-effect free. The only write endpoint is an ingest
run, and it is a dry run unless the caller says otherwise: an operator asking
"what would this batch extract" must not accidentally commit facts to a live
campaign. Facts are the authority other prompts are built from, so getting
them wrong is expensive and getting them wrong quietly is worse.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db.event_store import EventStore
from app.db.memory_fact_store import MemoryFactStore, default_db_path
from app.models.memory import FactStatus
from app.services.memory_ingestion import MemoryIngestionPipeline

router = APIRouter(tags=["memory"])

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fact_store() -> MemoryFactStore:
    return MemoryFactStore(default_db_path())


def _event_store() -> EventStore:
    path = os.environ.get("EVENT_DB_PATH", os.path.join(_BACKEND_DIR, "events.db"))
    return EventStore(path)


class FactOut(BaseModel):
    id: str
    campaign_id: str
    subject_id: str
    predicate: str
    object_value: str
    memory_type: str
    evidence_type: str
    status: str
    confidence: float
    importance: float
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    source_event_ids: List[str] = Field(default_factory=list)
    observer_ids: List[str] = Field(default_factory=list)
    supersedes_fact_id: Optional[str] = None
    recorded_at: Optional[str] = None


class EvidenceOut(BaseModel):
    id: str
    fact_id: str
    source_event_id: str
    evidence_type: str
    observer_id: str
    confidence: float
    recorded_at: Optional[str] = None


class IngestRequest(BaseModel):
    campaign_id: str
    limit: int = Field(default=40, ge=1, le=500)
    dry_run: bool = True

@router.get("/facts", response_model=List[FactOut])
def list_facts(
    campaign_id: str = Query(...),
    subject_id: Optional[str] = None,
    predicate: Optional[str] = None,
    status: Optional[str] = None,
    at: Optional[str] = None,
    observer: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    """List facts, newest first.

    Without `status` this returns only live facts, which is what a prompt or a
    UI wants: a superseded value is history, not an answer. Pass `status` for
    an audit view, or `at` to ask what was true at a given moment.

    `observer` applies the NPC knowledge filter here, on the way out, rather
    than trusting a caller to respect it: a fact witnessed only by the captain
    is not returned to whoever asks as the innkeeper.
    """
    store = _fact_store()
    try:
        statuses: Any = None
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            for value in statuses:
                if value not in {s.value for s in FactStatus}:
                    raise HTTPException(status_code=400, detail=f"unknown fact status: {value}")
        facts = store.find_facts(
            campaign_id,
            subject_id=subject_id,
            predicate=predicate,
            statuses=statuses,
            at=at,
            # Ask for more than the caller wanted: the filter runs after the
            # query, so a small limit could return nothing but hidden facts.
            limit=limit * 10 if observer else limit,
        )
        if observer:
            from app.services.memory_perspective import filter_facts_for_observer

            facts = filter_facts_for_observer(facts, observer)[:limit]
        return [FactOut(**_fact_dict(f)) for f in facts]
    finally:
        store.close()


@router.get("/facts/{fact_id}", response_model=FactOut)
def get_fact(fact_id: str):
    store = _fact_store()
    try:
        fact = store.get_fact(fact_id)
        if fact is None:
            raise HTTPException(status_code=404, detail="fact not found")
        return FactOut(**_fact_dict(fact))
    finally:
        store.close()


@router.get("/facts/{fact_id}/evidence", response_model=List[EvidenceOut])
def get_fact_evidence(fact_id: str):
    """Every recorded reason to believe this fact, oldest first."""
    store = _fact_store()
    try:
        if store.get_fact(fact_id) is None:
            raise HTTPException(status_code=404, detail="fact not found")
        return [
            EvidenceOut(
                id=e.id,
                fact_id=e.fact_id,
                source_event_id=e.source_event_id,
                evidence_type=str(getattr(e.evidence_type, "value", e.evidence_type)),
                observer_id=e.observer_id,
                confidence=e.confidence,
                recorded_at=e.recorded_at or None,
            )
            for e in store.get_evidence(fact_id)
        ]
    finally:
        store.close()


@router.get("/entities")
def list_entities(
    campaign_id: str = Query(...),
    kind: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    store = _fact_store()
    try:
        entities = store.list_entities(campaign_id, entity_type=kind, limit=limit)
        return [
            {
                "id": e.id,
                "canonical_name": e.canonical_name,
                "entity_type": e.entity_type,
                "aliases": list(e.aliases or []),
                "description": e.description,
            }
            for e in entities
        ]
    finally:
        store.close()


@router.get("/entities/resolve")
def resolve_entity(campaign_id: str = Query(...), name: str = Query(...)):
    """Resolve any known alias to its canonical entity, which is what query
    understanding needs before it can search anything."""
    store = _fact_store()
    try:
        entity = store.resolve_entity(campaign_id, name)
        if entity is None:
            raise HTTPException(status_code=404, detail="entity not found")
        return {
            "id": entity.id,
            "canonical_name": entity.canonical_name,
            "entity_type": entity.entity_type,
            "aliases": list(entity.aliases or []),
        }
    finally:
        store.close()

@router.get("/overview")
def overview(campaign_id: str = Query(...)):
    """Counts by status, for a health check and for the audit view."""
    store = _fact_store()
    try:
        return store.overview(campaign_id)
    finally:
        store.close()


@router.post("/ingest")
async def ingest(request: IngestRequest, llm: bool = Query(default=False)):
    """Run the ingestion pipeline once.

    `llm=false` (the default) uses only the deterministic extractor, so an
    operator can replay the pipeline offline with no model configured. Passing
    `llm=true` attaches the default router; a model failure degrades to the
    deterministic facts and is reported in `extractor_errors` instead of
    failing the call.
    """
    fact_store = _fact_store()
    event_store = _event_store()
    router_llm = None
    if llm:
        try:
            from app.engines.llm_router import LLMRouter, LLMConfig

            router_llm = LLMRouter(LLMConfig())
        except Exception:  # noqa: BLE001 - the pipeline works without a model
            router_llm = None
    try:
        pipeline = MemoryIngestionPipeline(event_store, fact_store, llm=router_llm)
        report = await pipeline.ingest(
            request.campaign_id, limit=request.limit, dry_run=request.dry_run,
        )
        return report.as_dict()
    finally:
        fact_store.close()


def _fact_dict(fact) -> Dict[str, Any]:
    return {
        "id": fact.id,
        "campaign_id": fact.campaign_id,
        "subject_id": fact.subject_id,
        "predicate": fact.predicate,
        "object_value": fact.object_value,
        "memory_type": str(getattr(fact.memory_type, "value", fact.memory_type)),
        "evidence_type": str(getattr(fact.evidence_type, "value", fact.evidence_type)),
        "status": str(getattr(fact.status, "value", fact.status)),
        "confidence": fact.confidence,
        "importance": fact.importance,
        "valid_from": fact.valid_from or None,
        "valid_until": fact.valid_until or None,
        "source_event_ids": list(fact.source_event_ids or []),
        "observer_ids": list(fact.observer_ids or []),
        "supersedes_fact_id": fact.supersedes_fact_id or None,
        "recorded_at": fact.recorded_at or None,
    }