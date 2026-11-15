"""The memory ingestion pipeline: events in, verified facts out.

The chain is deliberately split into stages that never share a write path:

    EventStore -> Event Collector -> LLM Fact Extractor -> Structured Fact
        -> Evidence Validator -> Conflict Detection -> MemoryFact -> Store

Nothing here trusts the model. The extractor *proposes* triples; the validator
decides what a proposal is worth from the event it came from; the conflict
detector decides whether it replaces something already known. A proposal the
model hallucinated from prose can therefore never reach ACTIVE on its own - it
stops at CANDIDATE with low confidence, which is visible but not authoritative.

Two more properties matter as much as correctness:

* idempotence - re-running over the same events must converge. Facts are keyed
  by content fingerprint and evidence is unique per (fact, event, type,
  observer), so a retried run adds nothing.
* the original events are never modified. Extraction reads; it does not write
  back into events.db.

Deterministic extraction runs first and needs no model at all: engine
settlements (inventory changes, combat results, power updates, time skips)
already carry structured payloads. The LLM extractor only sees what the
deterministic pass could not read, and is skipped entirely when no model is
configured.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.db.event_store import Event, EventStore, EventType
from app.db.memory_fact_store import MemoryFactStore
from app.models.memory import (
    AUTO_ACTIVE,
    EVIDENCE_TRUST,
    EvidenceType,
    FactStatus,
    LIVE_STATUSES,
    MemoryEntity,
    MemoryEvidence,
    MemoryFact,
    MemoryType,
    can_reach_active,
    evidence_type_of_event,
    is_scoped,
    normalize_alias,
    trust_of,
)

logger = logging.getLogger(__name__)

#: The player is an entity like any other, but it has no NPC roster entry, so
#: it is named by a constant instead of looked up by name.
PLAYER_ID = "player"

#: Event types the fact pipeline consumes. Deliberately narrower than "every
#: event": crystal events are derived from other events, and ingesting them
#: would feed summaries back into the fact store until hallucination compounds.
INGESTIBLE_EVENT_TYPES: Tuple[EventType, ...] = (
    EventType.PLAYER_ACTION,
    EventType.COMBAT_RESULT,
    EventType.INVENTORY,
    EventType.POWER_LEVEL_UPDATE,
    EventType.WORLD_TICK,
    EventType.TIMESKIP,
    EventType.NARRATOR_RESPONSE,
    EventType.NPC_THOUGHT,
)

#: Events whose payload already *is* a structured claim. These never need a
#: model, so they keep working with no LLM configured at all.
DETERMINISTIC_EVENT_TYPES: frozenset = frozenset(
    {
        EventType.COMBAT_RESULT,
        EventType.INVENTORY,
        EventType.POWER_LEVEL_UPDATE,
        EventType.TIMESKIP,
    }
)

#: How many events one ingest call consumes by default. Bounded so a long
#: campaign is caught up in slices rather than in one enormous transaction.
DEFAULT_BATCH = 40

#: Corroboration needed before a claim is trusted beyond its own evidence type.
VERIFY_MIN_CONFIDENCE = 0.75
VERIFY_MIN_EVIDENCE = 2

#: Predicates that describe an ongoing state and are replaced when they change,
#: rather than accumulating. "current_location" describes the present; a combat
#: outcome is a thing that happened and stays true forever.
MUTABLE_PREDICATES: frozenset = frozenset(
    {
        "current_location",
        "power_level",
        "status",
        "carrying",
        "relationship",
        "mood",
        "goal",
    }
)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _text_of(event: Event, limit: int = 400) -> str:
    """The prose or content an event carries, whichever field it uses."""
    payload = event.payload or {}
    for key in ("text", "content", "narrative", "action", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:limit]
    return ""


def text_of_event_short(event: Event, limit: int = 200) -> str:
    return _text_of(event, limit=limit)


def _clamp(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


@dataclass
class FactProposal:
    """One candidate claim, before the validator decides what it is worth."""

    subject_id: str
    predicate: str
    object_value: str
    evidence_type: str
    memory_type: str = MemoryType.SEMANTIC.value
    confidence: float = 0.5
    importance: float = 0.5
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    observer_ids: List[str] = field(default_factory=list)
    source_event_id: str = ""
    evidence_content: str = ""
    origin: str = "deterministic"

    def fingerprint_seed(self) -> Tuple[str, str, str, Optional[str]]:
        return (self.subject_id, self.predicate, self.object_value, self.valid_from)


@dataclass
class IngestReport:
    """What one ingest run did. Returned to callers and to the API."""

    campaign_id: str
    consumed_events: int = 0
    proposals: int = 0
    facts_created: int = 0
    facts_reinforced: int = 0
    candidates: int = 0
    activated: int = 0
    disputed: int = 0
    superseded: int = 0
    rejected: int = 0
    skipped_duplicate: int = 0
    extractor_errors: List[str] = field(default_factory=list)
    dry_run: bool = False
    fact_ids: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "consumed_events": self.consumed_events,
            "proposals": self.proposals,
            "facts_created": self.facts_created,
            "facts_reinforced": self.facts_reinforced,
            "candidates": self.candidates,
            "activated": self.activated,
            "disputed": self.disputed,
            "superseded": self.superseded,
            "rejected": self.rejected,
            "skipped_duplicate": self.skipped_duplicate,
            "extractor_errors": self.extractor_errors,
            "dry_run": self.dry_run,
            "fact_ids": self.fact_ids,
        }

# -- deterministic extractor ----------------------------------------


class DeterministicExtractor:
    """Reads structured payloads. No model, no guessing, no network.

    This is the half of extraction that must keep working when the LLM is
    slow, unavailable or lying: an inventory event says which item entered the
    bag, a combat result says who won.
    """

    def extract(self, event: Event) -> List[FactProposal]:
        handler = {
            EventType.INVENTORY: self._inventory,
            EventType.COMBAT_RESULT: self._combat,
            EventType.POWER_LEVEL_UPDATE: self._power,
            EventType.TIMESKIP: self._timeskip,
            EventType.PLAYER_ACTION: self._player_action_location,
        }.get(event.event_type)
        if handler is None:
            return []
        try:
            return handler(event)
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("deterministic extraction skipped %s: %s", event.id, exc)
            return []

    def _inventory(self, event: Event) -> List[FactProposal]:
        payload = event.payload or {}
        action = str(payload.get("action", "")).strip().lower()
        name = str(payload.get("name", "")).strip()
        if not name:
            return []
        predicate = {
            "add": "carrying",
            "use": "used_item",
            "lose": "lost_item",
        }.get(action)
        if predicate is None:
            return []
        return [
            FactProposal(
                subject_id=PLAYER_ID,
                predicate=predicate,
                object_value=name,
                evidence_type=EvidenceType.ENGINE_STATE.value,
                confidence=1.0,
                importance=0.45 if action == "add" else 0.3,
                source_event_id=event.id,
                evidence_content=json.dumps(payload, ensure_ascii=False)[:400],
            )
        ]

    def _combat(self, event: Event) -> List[FactProposal]:
        payload = event.payload or {}
        opponent = str(payload.get("opponent_name", "") or "").strip()
        outcome = str(payload.get("outcome", "") or "").strip()
        if not opponent or not outcome:
            return []
        proposals = [
            FactProposal(
                subject_id=PLAYER_ID,
                predicate="combat_outcome",
                object_value=f"{outcome} vs {opponent}",
                evidence_type=EvidenceType.COMMITTED_ACTION.value,
                confidence=1.0,
                importance=0.7,
                source_event_id=event.id,
                evidence_content=json.dumps(payload, ensure_ascii=False)[:400],
            )
        ]
        power = payload.get("opponent_power")
        if isinstance(power, (int, float)):
            proposals.append(
                FactProposal(
                    subject_id=opponent,
                    predicate="power_level",
                    object_value=str(int(power)),
                    evidence_type=EvidenceType.COMMITTED_ACTION.value,
                    confidence=1.0,
                    importance=0.55,
                    source_event_id=event.id,
                    evidence_content="combat_result",
                )
            )
        return proposals

    def _power(self, event: Event) -> List[FactProposal]:
        payload = event.payload or {}
        target = str(payload.get("target", "") or "").strip() or PLAYER_ID
        new_power = payload.get("new_power")
        if not isinstance(new_power, (int, float)):
            return []
        return [
            FactProposal(
                subject_id=target,
                predicate="power_level",
                object_value=str(int(new_power)),
                evidence_type=EvidenceType.ENGINE_STATE.value,
                confidence=1.0,
                importance=0.5,
                source_event_id=event.id,
                evidence_content="power_level_update",
            )
        ]

    def _timeskip(self, event: Event) -> List[FactProposal]:
        seconds = event.payload.get("seconds")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return []
        return [
            FactProposal(
                subject_id=PLAYER_ID,
                predicate="time_skipped_seconds",
                object_value=str(int(seconds)),
                evidence_type=EvidenceType.ENGINE_STATE.value,
                confidence=1.0,
                importance=0.05,
                source_event_id=event.id,
                evidence_content="timeskip",
            )
        ]

    def _player_action_location(self, event: Event) -> List[FactProposal]:
        location = str(event.location or "").strip()
        if not location or location.lower() == "current":
            return []
        return [
            FactProposal(
                subject_id=PLAYER_ID,
                predicate="current_location",
                object_value=location,
                evidence_type=EvidenceType.COMMITTED_ACTION.value,
                confidence=0.95,
                importance=0.5,
                valid_from=event.created_at,
                source_event_id=event.id,
                evidence_content=location,
            )
        ]


_EXTRACT_PROMPT = """You extract structured facts from a single event of an interactive story.

Return ONLY a JSON array. Each item is an object with these keys:
  "subject": who or what the fact is about (a character, place, item or faction name as written)
  "predicate": a short snake_case relation, e.g. identity, lives_in, owns, relationship, goal, ability
  "object": the value, in a few words
  "confidence": 0.0-1.0, how certain the event makes this
  "importance": 0.0-1.0, how much a player would care to remember it
  "observer": the NPC who directly witnessed it, or "" if it is public

Rules:
- Only facts this event actually establishes. Do not infer, complete or embellish.
- If the text does not clearly establish a fact, return [].
- Never turn a guess, a hope or a rumour into a fact; use predicate "suspected_<x>" if the event only reports a suspicion.
- Return the JSON array and nothing else.

EVENT TYPE: {event_type}
LOCATION: {location}
TEXT:
{text}
"""


class LLMFactExtractor:
    """Proposes triples from prose. Never commits anything.

    The model is a proposal generator: its output is parsed, clamped and then
    given the evidence weight of the event it came from (narrative prose =>
    LLM_NARRATIVE), so a confident hallucination still cannot reach ACTIVE.
    """

    def __init__(self, llm: Any, *, max_proposals: int = 6):
        self._llm = llm
        self._max_proposals = max_proposals

    async def extract(self, event: Event) -> List[FactProposal]:
        text = _text_of(event)
        if not text or self._llm is None:
            return []
        prompt = _EXTRACT_PROMPT.format(
            event_type=getattr(event.event_type, "value", event.event_type),
            location=event.location or "",
            text=text,
        )
        try:
            raw = await self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
            )
        except Exception as exc:  # noqa: BLE001 - an extractor failure must not stop ingestion
            logger.info("fact extraction failed for event %s: %s", event.id, exc)
            raise
        return self.parse(raw, event)

    def parse(self, raw: Any, event: Event) -> List[FactProposal]:
        payload = self._loads(raw)
        if not isinstance(payload, list):
            return []
        evidence = evidence_type_of_event(event.event_type)
        proposals: List[FactProposal] = []
        for item in payload[: self._max_proposals]:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject", "") or "").strip()
            predicate = re.sub(r"[^0-9a-zA-Z_]+", "_", str(item.get("predicate", "") or "")).strip("_").lower()
            obj = str(item.get("object", "") or "").strip()
            if not subject or not predicate:
                # A half-formed triple is not a fact; leave it out entirely
                # rather than storing a subject with no claim attached.
                continue
            observer = str(item.get("observer", "") or "").strip()
            proposals.append(
                FactProposal(
                    subject_id=subject,
                    predicate=predicate,
                    object_value=obj or "UNKNOWN",
                    evidence_type=evidence.value,
                    confidence=_clamp(item.get("confidence"), trust_of(evidence)),
                    importance=_clamp(item.get("importance"), 0.4),
                    observer_ids=[observer] if observer else [],
                    source_event_id=event.id,
                    evidence_content=text_of_event_short(event),
                    origin="llm",
                )
            )
        return proposals

    @staticmethod
    def _loads(raw: Any) -> Any:
        if isinstance(raw, (list, dict)):
            return raw
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if not match:
                return None
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
# -- validator ------------------------------------------------------


@dataclass
class ValidatedProposal:
    """A proposal after the validator decided what it may do."""

    proposal: FactProposal
    may_activate: bool
    status: str
    reason: str = ""


class EvidenceValidator:
    """Decides what a proposal is worth, from the evidence behind it.

    This is the gate that keeps the world's truth clean:

    * engine settlements and committed actions may reach ACTIVE;
    * observations and statements may not - they become CANDIDATE, and only
      corroboration (several independent pieces of evidence) promotes them;
    * prose is never enough, no matter how confident the extractor was;
    * an observer-scoped claim must name its observer, otherwise it is treated
      as public *information*, never as world truth.
    """

    def __init__(self, store: MemoryFactStore):
        self._store = store

    def validate(self, proposal: FactProposal) -> ValidatedProposal:
        evidence = proposal.evidence_type
        if not proposal.subject_id.strip() or not proposal.predicate.strip():
            return ValidatedProposal(proposal, False, FactStatus.REJECTED.value, "incomplete triple")

        if is_scoped(evidence) and not proposal.observer_ids:
            # An observation with no named observer would leak to everyone.
            # Keep it as a candidate so it is visible, but never authoritative.
            return ValidatedProposal(
                proposal,
                False,
                FactStatus.CANDIDATE.value,
                "scoped evidence without an observer",
            )

        if can_reach_active(evidence):
            return ValidatedProposal(proposal, True, FactStatus.ACTIVE.value, "settled evidence")

        return ValidatedProposal(
            proposal,
            False,
            FactStatus.CANDIDATE.value,
            "needs corroboration",
        )

    def corroboration_reaches_verification(self, fact: MemoryFact) -> bool:
        """Whether accumulated evidence is enough to stop being a candidate."""
        evidence = self._store.get_evidence(fact.id)
        if not evidence:
            return False
        if any(item.evidence_type in AUTO_ACTIVE for item in evidence):
            return True
        independent_events = {item.source_event_id for item in evidence if item.source_event_id}
        best_trust = max((trust_of(item.evidence_type) for item in evidence), default=0.0)
        return (
            len(independent_events) >= VERIFY_MIN_EVIDENCE
            and best_trust >= EVIDENCE_TRUST[EvidenceType.NPC_OBSERVATION.value]
            and fact.confidence >= VERIFY_MIN_CONFIDENCE
        )


# -- conflict detection ---------------------------------------------


class ConflictDetector:
    """Finds facts this proposal contradicts.

    Only predicates that describe an ongoing state are treated as replaceable:
    a location, a power level, a relationship. An event that happened keeps
    happening, so a second combat outcome does not supersede the first.
    """

    MUTABLE = MUTABLE_PREDICATES

    def __init__(self, store: MemoryFactStore):
        self._store = store

    def find_conflicts(self, fact: MemoryFact) -> List[MemoryFact]:
        if fact.predicate not in self.MUTABLE:
            return []
        live = self._store.find_facts(
            fact.campaign_id,
            subject_id=fact.subject_id,
            predicate=fact.predicate,
            limit=50,
        )
        conflicts: List[MemoryFact] = []
        for other in live:
            if other.id == fact.id:
                continue
            if normalize_alias(other.object_value) == normalize_alias(fact.object_value):
                continue
            conflicts.append(other)
        return conflicts


# -- pipeline -------------------------------------------------------


class MemoryIngestionPipeline:
    """Turns events into facts. The only writer of the fact store.

    Every public method is safe to call twice: the pipeline converges on the
    same state whether it ran once with 80 events or twice with 40.
    """

    def __init__(
        self,
        event_store: EventStore,
        fact_store: MemoryFactStore,
        *,
        llm: Any = None,
        deterministic: Optional[DeterministicExtractor] = None,
        llm_extractor: Optional[LLMFactExtractor] = None,
    ):
        self._events = event_store
        self._facts = fact_store
        self._deterministic = deterministic or DeterministicExtractor()
        if llm_extractor is not None:
            self._llm_extractor = llm_extractor
        else:
            self._llm_extractor = LLMFactExtractor(llm) if llm is not None else None
        self._validator = EvidenceValidator(fact_store)
        self._conflicts = ConflictDetector(fact_store)

    # -- collection -------------------------------------------------

    def collect(
        self, campaign_id: str, *, limit: int = DEFAULT_BATCH, after: Optional[str] = None
    ) -> List[Event]:
        """Events not yet consumed by the fact pipeline, oldest first."""
        cursor = after if after is not None else self._facts.get_cursor(campaign_id)["last_event_created_at"]
        events = self._events.get_after(
            campaign_id,
            after_created_at=cursor or None,
            limit=max(limit, 1) * 3,
            event_types=list(INGESTIBLE_EVENT_TYPES),
        )
        # get_after is strictly greater-than, so the stored cursor already
        # means 'consumed up to and including'. The slice keeps one batch small
        # even when many events share a second.
        return events[: max(limit, 1)]

    # -- extraction -------------------------------------------------

    async def propose(self, events: Sequence[Event]) -> Tuple[List[FactProposal], List[str]]:
        """All proposals for a batch, plus extractor errors (never raised).

        A failing LLM call costs that event's prose facts, not the run: the
        deterministic proposals still go through, which is why a campaign
        stays consistent with no model configured.
        """
        proposals: List[FactProposal] = []
        errors: List[str] = []
        for event in events:
            proposals.extend(self._deterministic.extract(event))
            if self._llm_extractor is None:
                continue
            if event.event_type in DETERMINISTIC_EVENT_TYPES and not _text_of(event):
                continue
            try:
                proposals.extend(await self._llm_extractor.extract(event))
            except Exception as exc:  # noqa: BLE001 - degrade, do not abort
                errors.append(f"{event.id}: {type(exc).__name__}: {exc}")
        return proposals, errors

    # -- commit -----------------------------------------------------

    def commit(
        self, campaign_id: str, proposal: FactProposal, report: IngestReport
    ) -> Optional[MemoryFact]:
        """Turn one validated proposal into stored fact state."""
        verdict = self._validator.validate(proposal)
        if verdict.status == FactStatus.REJECTED.value:
            report.rejected += 1
            return None

        fact = MemoryFact(
            campaign_id=campaign_id,
            subject_id=proposal.subject_id,
            predicate=proposal.predicate,
            object_value=proposal.object_value,
            memory_type=proposal.memory_type,
            evidence_type=proposal.evidence_type,
            confidence=_clamp(proposal.confidence, trust_of(proposal.evidence_type)),
            importance=_clamp(proposal.importance, 0.4),
            status=FactStatus.EXTRACTED.value,
            valid_from=proposal.valid_from,
            valid_until=proposal.valid_until,
            source_event_ids=[proposal.source_event_id] if proposal.source_event_id else [],
            observer_ids=list(proposal.observer_ids),
            recorded_at=_now(),
        )

        stored, created = self._facts.add_fact(fact)
        if created:
            report.facts_created += 1
        else:
            report.facts_reinforced += 1

        evidence = MemoryEvidence(
            fact_id=stored.id,
            campaign_id=campaign_id,
            source_event_id=proposal.source_event_id,
            evidence_type=proposal.evidence_type,
            observer_id=(proposal.observer_ids[0] if proposal.observer_ids else ""),
            confidence=_clamp(proposal.confidence, trust_of(proposal.evidence_type)),
            evidence_content=proposal.evidence_content[:1000],
        )
        _, evidence_created = self._facts.add_evidence(evidence)
        if not created and not evidence_created:
            # Nothing new was learned: same claim, same event, same witness.
            report.skipped_duplicate += 1
            return stored

        if proposal.source_event_id:
            self._facts.add_source_event(stored.id, proposal.source_event_id)
        if proposal.observer_ids:
            self._facts.add_observers(stored.id, proposal.observer_ids)
            self._note_entities(campaign_id, proposal.observer_ids, "person")

        # Entities come from the triple itself, so alias resolution has
        # something to work with the moment the fact exists.
        self._note_entities(campaign_id, [proposal.subject_id], "unknown")

        return self._advance(stored.id, report, verdict)

    def _advance(
        self, fact_id: str, report: IngestReport, verdict: ValidatedProposal
    ) -> Optional[MemoryFact]:
        """Walk a fact as far along the lifecycle as its evidence allows."""
        fact = self._facts.get_fact(fact_id)
        if fact is None:
            return None
        if fact.status not in (FactStatus.EXTRACTED.value, FactStatus.CANDIDATE.value):
            return fact

        # EXTRACTED -> CANDIDATE is unconditional: the pipeline now holds a
        # structured claim, which is exactly what CANDIDATE means.
        if fact.status == FactStatus.EXTRACTED.value:
            fact = self._facts.advance_status(fact_id, FactStatus.CANDIDATE.value)
            report.candidates += 1

        may_verify = verdict.may_activate or self._validator.corroboration_reaches_verification(fact)
        if not may_verify:
            return fact

        fact = self._facts.advance_status(fact_id, FactStatus.VERIFIED.value)
        if not verdict.may_activate:
            # Corroborated but not settled: readable verified knowledge that is
            # not yet the world's current answer.
            return fact

        conflicts = self._conflicts.find_conflicts(fact)
        if self._open_conflicts(fact, conflicts):
            fact = self._facts.advance_status(fact_id, FactStatus.DISPUTED.value)
            report.disputed += 1
            return fact

        fact = self._facts.advance_status(fact_id, FactStatus.ACTIVE.value)
        report.activated += 1
        for other in conflicts:
            if other.status in LIVE_STATUSES and other.id != fact.id:
                try:
                    self._facts.supersede(other.id, fact.id)
                    self._facts.link_supersedes(fact.id, other.id)
                    report.superseded += 1
                except (KeyError, ValueError) as exc:
                    logger.info("could not supersede %s: %s", other.id, exc)
        return self._facts.get_fact(fact_id)

    def _open_conflicts(
        self, fact: MemoryFact, conflicts: Sequence[MemoryFact]
    ) -> List[MemoryFact]:
        """Conflicts the new fact is not strong enough to settle.

        Equal authority conflicting with a fact recorded at the same instant is
        a dispute, not a silent overwrite: two settlements that disagree mean
        the state is genuinely unclear and a later event has to break the tie.
        """
        strong = trust_of(fact.evidence_type)
        unresolved: List[MemoryFact] = []
        for other in conflicts:
            if other.status != FactStatus.ACTIVE.value:
                continue
            if trust_of(other.evidence_type) < strong:
                continue
            if other.created_at and fact.recorded_at and other.created_at >= fact.recorded_at:
                unresolved.append(other)
        return unresolved

    def _note_entities(self, campaign_id: str, names: Iterable[str], entity_type: str) -> None:
        for name in names:
            cleaned = str(name or "").strip()
            if not cleaned or cleaned == PLAYER_ID:
                continue
            try:
                self._facts.upsert_entity(
                    MemoryEntity(
                        campaign_id=campaign_id,
                        canonical_name=cleaned,
                        entity_type=entity_type,
                    )
                )
            except (ValueError, TypeError) as exc:
                logger.debug("entity upsert skipped for %r: %s", cleaned, exc)

    # -- public entry points ----------------------------------------

    async def ingest(
        self,
        campaign_id: str,
        *,
        limit: int = DEFAULT_BATCH,
        dry_run: bool = False,
        after: Optional[str] = None,
    ) -> IngestReport:
        """Consume a batch of events and return what changed.

        With dry_run the extraction and validation still run, so the caller can
        preview what would be written, but the store is not touched and the
        cursor does not move.
        """
        report = IngestReport(campaign_id=campaign_id, dry_run=dry_run)
        events = self.collect(campaign_id, limit=limit, after=after)
        report.consumed_events = len(events)
        if not events:
            return report

        proposals, errors = await self.propose(events)
        report.proposals = len(proposals)
        report.extractor_errors = errors

        if dry_run:
            for proposal in proposals:
                verdict = self._validator.validate(proposal)
                if verdict.status == FactStatus.REJECTED.value:
                    report.rejected += 1
                elif verdict.may_activate:
                    report.activated += 1
                else:
                    report.candidates += 1
            return report

        for proposal in proposals:
            try:
                fact = self.commit(campaign_id, proposal, report)
            except (KeyError, ValueError, TypeError) as exc:
                # One bad proposal must not lose the whole batch.
                logger.info("proposal dropped (%s): %s", type(exc).__name__, exc)
                report.extractor_errors.append(f"{proposal.predicate}: {exc}")
                continue
            if fact is not None and fact.id not in report.fact_ids:
                report.fact_ids.append(fact.id)

        last = events[-1]
        self._facts.set_cursor(
            campaign_id,
            last.created_at,
            last.id,
            processed_events=len(events),
        )
        return report

    async def ingest_event(
        self, campaign_id: str, event: Event, report: Optional[IngestReport] = None
    ) -> IngestReport:
        """Ingest exactly one event, for callers that already hold it."""
        report = report or IngestReport(campaign_id=campaign_id)
        proposals, errors = await self.propose([event])
        report.consumed_events += 1
        report.proposals += len(proposals)
        report.extractor_errors.extend(errors)
        for proposal in proposals:
            try:
                fact = self.commit(campaign_id, proposal, report)
            except (KeyError, ValueError, TypeError) as exc:
                report.extractor_errors.append(f"{proposal.predicate}: {exc}")
                continue
            if fact is not None and fact.id not in report.fact_ids:
                report.fact_ids.append(fact.id)
        return report


def build_pipeline(
    event_store: EventStore, fact_store: MemoryFactStore, llm: Any = None
) -> MemoryIngestionPipeline:
    """Convenience constructor used by the routes and the app wiring."""
    return MemoryIngestionPipeline(event_store, fact_store, llm=llm)