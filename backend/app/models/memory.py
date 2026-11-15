"""Structured memory: facts, the evidence behind them, and their lifecycle.

Two ideas carry this module:

* a fact is never just "text the model wrote". It is a triple plus the
  provenance that justifies it, and the provenance decides how far it may
  travel: an engine settlement can reach ACTIVE, a narrator paragraph cannot;
* a fact is never overwritten. When a newer fact contradicts an older one the
  older one becomes SUPERSEDED and keeps its id, its evidence and its validity
  window, so "who was the enemy in chapter two" stays answerable forever.

The enums here are the only vocabulary the rest of the memory stack uses, so
`MemoryFact`, the ingestion pipeline, the retrieval layer and the context pack
cannot quietly drift apart.
"""
from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: What an extractor could not determine. Never guessed at, never filled in by
#: a model: an unknown predicate stays unknown and the fact stays a candidate.
UNKNOWN = "UNKNOWN"


class EvidenceType(str, Enum):
    """Where a claim came from, in descending order of authority.

    The order is load-bearing: `can_reach_active` reads it to decide whether a
    claim may become world truth without a human accepting it.
    """

    ENGINE_STATE = "engine_state"
    AUTHOR_CANON = "author_canon"
    COMMITTED_ACTION = "committed_action"
    NPC_OBSERVATION = "npc_observation"
    NPC_DIALOGUE = "npc_dialogue"
    PLAYER_STATEMENT = "player_statement"
    INFERRED_FACT = "inferred_fact"
    LLM_NARRATIVE = "llm_narrative"


#: How much a claim of this kind is worth before any corroboration.
EVIDENCE_TRUST: Dict[str, float] = {
    EvidenceType.ENGINE_STATE.value: 1.0,
    EvidenceType.AUTHOR_CANON.value: 1.0,
    EvidenceType.COMMITTED_ACTION.value: 0.95,
    EvidenceType.NPC_OBSERVATION.value: 0.80,
    EvidenceType.NPC_DIALOGUE.value: 0.50,
    EvidenceType.PLAYER_STATEMENT.value: 0.40,
    EvidenceType.INFERRED_FACT.value: 0.30,
    EvidenceType.LLM_NARRATIVE.value: 0.20,
}

#: Evidence kinds that may reach ACTIVE on their own. Everything else stops at
#: CANDIDATE and waits for corroboration or for a human to accept it, which is
#: what keeps prose and player guesses out of the world's truth.
AUTO_ACTIVE: frozenset[str] = frozenset(
    {
        EvidenceType.ENGINE_STATE.value,
        EvidenceType.AUTHOR_CANON.value,
        EvidenceType.COMMITTED_ACTION.value,
    }
)

#: Evidence that may be true *for one observer* without being world truth.
#: A fact built from these must name its observer, or it is treated as public.
SCOPED_EVIDENCE: frozenset[str] = frozenset(
    {
        EvidenceType.NPC_OBSERVATION.value,
        EvidenceType.NPC_DIALOGUE.value,
        EvidenceType.INFERRED_FACT.value,
    }
)

#: Labels used when a fact is rendered into a prompt, so the model can tell a
#: settled fact from a claim somebody merely made.
EVIDENCE_LABELS_ZH: Dict[str, str] = {
    EvidenceType.ENGINE_STATE.value: "引擎状态",
    EvidenceType.AUTHOR_CANON.value: "作者设定",
    EvidenceType.COMMITTED_ACTION.value: "已结算行动",
    EvidenceType.NPC_OBSERVATION.value: "亲眼所见",
    EvidenceType.NPC_DIALOGUE.value: "口述传闻",
    EvidenceType.PLAYER_STATEMENT.value: "玩家声称",
    EvidenceType.INFERRED_FACT.value: "推断",
    EvidenceType.LLM_NARRATIVE.value: "叙事描述",
}

#: Event type -> the evidence kind it can justify. An event type that is not
#: here yields LLM_NARRATIVE: the safe assumption is always "this is prose".
EVIDENCE_FROM_EVENT: Dict[str, str] = {
    "PLAYER_ACTION": EvidenceType.COMMITTED_ACTION.value,
    "COMBAT_RESULT": EvidenceType.COMMITTED_ACTION.value,
    "INVENTORY": EvidenceType.ENGINE_STATE.value,
    "INVENTORY_PROPOSAL": EvidenceType.LLM_NARRATIVE.value,
    "POWER_LEVEL_UPDATE": EvidenceType.ENGINE_STATE.value,
    "WORLD_TICK": EvidenceType.ENGINE_STATE.value,
    "TIMESKIP": EvidenceType.ENGINE_STATE.value,
    "NPC_THOUGHT": EvidenceType.INFERRED_FACT.value,
    "NARRATOR_RESPONSE": EvidenceType.LLM_NARRATIVE.value,
    "PLOT_GENERATION": EvidenceType.LLM_NARRATIVE.value,
    "JOURNAL_ENTRY": EvidenceType.LLM_NARRATIVE.value,
    "MEMORY_CRYSTAL": EvidenceType.LLM_NARRATIVE.value,
    "AI_OPENING_GENERATED": EvidenceType.LLM_NARRATIVE.value,
}


def evidence_type_of_event(event_type: Any) -> EvidenceType:
    """The evidence kind an event can justify; prose when unknown."""
    key = getattr(event_type, "value", event_type)
    raw = EVIDENCE_FROM_EVENT.get(str(key or "").strip())
    if raw:
        return EvidenceType(raw)
    return EvidenceType.LLM_NARRATIVE


def trust_of(evidence_type: Any) -> float:
    key = getattr(evidence_type, "value", evidence_type)
    return EVIDENCE_TRUST.get(str(key or ""), EVIDENCE_TRUST[EvidenceType.LLM_NARRATIVE.value])


def can_reach_active(evidence_type: Any) -> bool:
    """True when this kind of evidence may become world truth unaided."""
    key = getattr(evidence_type, "value", evidence_type)
    return str(key or "") in AUTO_ACTIVE


def is_scoped(evidence_type: Any) -> bool:
    key = getattr(evidence_type, "value", evidence_type)
    return str(key or "") in SCOPED_EVIDENCE


class MemoryType(str, Enum):
    """The five kinds of memory the engine keeps apart.

    WORKING lives for a scene and is never persisted as a fact; EPISODIC is the
    existing SHORT/MEDIUM/LONG crystal ladder; SEMANTIC is this fact store;
    PROCEDURAL is a fact about how something behaves; CANON is authored truth
    that model output may never overwrite.
    """

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    CANON = "canon"


class FactStatus(str, Enum):
    """Lifecycle of one fact. Older states are kept, never deleted."""

    EXTRACTED = "extracted"
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DISPUTED = "disputed"
    REJECTED = "rejected"
    RETRACTED = "retracted"


#: Every legal move, one hop at a time. A fact cannot skip verification, and
#: nothing that was rejected or retracted comes back to life - a new fact is
#: written instead, which keeps the audit trail honest.
#:
#: Callers that own a fact (the ingestion pipeline) advance it hop by hop;
#: MemoryFactStore.update_status refuses anything that is not on this table so
#: the guarantee cannot be side-stepped by a convenience call.
ALLOWED_TRANSITIONS: Dict[str, frozenset] = {
    FactStatus.EXTRACTED.value: frozenset(
        {FactStatus.CANDIDATE.value, FactStatus.REJECTED.value}
    ),
    FactStatus.CANDIDATE.value: frozenset(
        {FactStatus.VERIFIED.value, FactStatus.DISPUTED.value, FactStatus.REJECTED.value}
    ),
    FactStatus.VERIFIED.value: frozenset(
        {FactStatus.ACTIVE.value, FactStatus.DISPUTED.value, FactStatus.REJECTED.value}
    ),
    FactStatus.ACTIVE.value: frozenset(
        {FactStatus.SUPERSEDED.value, FactStatus.DISPUTED.value, FactStatus.RETRACTED.value}
    ),
    FactStatus.DISPUTED.value: frozenset(
        {FactStatus.VERIFIED.value, FactStatus.REJECTED.value, FactStatus.RETRACTED.value}
    ),
    FactStatus.SUPERSEDED.value: frozenset({FactStatus.RETRACTED.value}),
    FactStatus.REJECTED.value: frozenset(),
    FactStatus.RETRACTED.value: frozenset(),
}

#: Statuses a reader may see without asking for history.
LIVE_STATUSES: frozenset[str] = frozenset(
    {FactStatus.VERIFIED.value, FactStatus.ACTIVE.value, FactStatus.DISPUTED.value}
)


def can_transition(current: Any, target: Any) -> bool:
    """Whether a fact may move from one status to another."""
    source = str(getattr(current, "value", current) or "").strip()
    dest = str(getattr(target, "value", target) or "").strip()
    return dest in ALLOWED_TRANSITIONS.get(source, frozenset())


def transition_path(current: Any, target: Any) -> List[str]:
    """Shortest legal route from one status to another, or [] when none exists.

    The ingestion pipeline needs this: a claim backed by engine settlement is
    allowed to end up ACTIVE, but it still has to *walk* EXTRACTED -> CANDIDATE
    -> VERIFIED -> ACTIVE rather than appear there, so the lifecycle has exactly
    one meaning whether a fact arrived from an engine event or a model proposal.

    This is a planning helper, not a permission slip: each hop it returns is
    still checked against can_transition when it is written.
    """
    source = str(getattr(current, "value", current) or "").strip()
    dest = str(getattr(target, "value", target) or "").strip()
    if not source or not dest:
        return []
    if source == dest:
        return [source]
    queue: List[List[str]] = [[source]]
    seen = {source}
    while queue:
        path = queue.pop(0)
        for nxt in sorted(ALLOWED_TRANSITIONS.get(path[-1], frozenset())):
            if nxt in seen:
                continue
            extended = path + [nxt]
            if nxt == dest:
                return extended
            if nxt in (FactStatus.REJECTED.value, FactStatus.RETRACTED.value):
                continue
            seen.add(nxt)
            queue.append(extended)
    return []


def normalize_alias(name: str) -> str:
    """Comparison form of a name: NFKC, casefold, collapsed spaces.

    Full-width and half-width spellings, Latin case and stray punctuation all
    fold together, so "Eileen", "ｅｉｌｅｅｎ" and " Eileen " are one entity.
    """
    text = unicodedata.normalize("NFKC", str(name or "")).strip().casefold()
    return re.sub(r"\s+", " ", text)


class MemoryFact(BaseModel):
    """One structured assertion: subject - predicate - object, with provenance.

    `valid_from`/`valid_until` are narrative timestamps (ISO 8601), not wall
    clock: "Eileen is an enemy" is not false in chapter three, it is *out of
    scope* there. A reader asks for the fact that was valid *at* a moment.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True, validate_default=True)

    id: str = ""
    campaign_id: str
    subject_id: str
    predicate: str
    object_value: str = UNKNOWN

    memory_type: MemoryType = MemoryType.SEMANTIC
    evidence_type: EvidenceType = EvidenceType.INFERRED_FACT

    confidence: float = 0.5
    importance: float = 0.5
    status: FactStatus = FactStatus.CANDIDATE

    valid_from: Optional[str] = None
    valid_until: Optional[str] = None

    source_event_ids: List[str] = Field(default_factory=list)
    observer_ids: List[str] = Field(default_factory=list)

    supersedes_fact_id: Optional[str] = None

    #: When the engine learned this, as opposed to when the claim holds. A fact
    #: recorded late about an early event keeps the early valid_from and a late
    #: recorded_at, which is how a retraction can be distinguished from a lie.
    recorded_at: str = ""

    created_at: str = ""
    updated_at: str = ""

    @field_validator("subject_id", "predicate")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("subject_id and predicate must not be blank")
        return text

    @field_validator("confidence", "importance")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @field_validator("object_value")
    @classmethod
    def _object_or_unknown(cls, value: str) -> str:
        text = str(value or "").strip()
        return text or UNKNOWN

    def is_current(self, at: Optional[str] = None) -> bool:
        """Whether this fact was the truth at `at` (default: now, i.e. no end)."""
        if self.status in (FactStatus.REJECTED.value, FactStatus.RETRACTED.value):
            return False
        if at is None:
            return self.valid_until is None
        if self.valid_from and at < self.valid_from:
            return False
        if self.valid_until and at >= self.valid_until:
            return False
        return True

    def render_line(self) -> str:
        """One prompt line: the claim, then who says so and how strongly."""
        label = EVIDENCE_LABELS_ZH.get(str(self.evidence_type), str(self.evidence_type))
        scope = ""
        if self.observer_ids:
            scope = " 仅 %s 知道" % "、".join(self.observer_ids[:3])
        window = ""
        if self.valid_from or self.valid_until:
            window = " [%s ~ %s]" % (self.valid_from or "?", self.valid_until or "现在")
        return "- %s | %s = %s （%s，置信 %.2f%s%s）" % (
            self.subject_id,
            self.predicate,
            self.object_value,
            label,
            self.confidence,
            scope,
            window,
        )


class MemoryEvidence(BaseModel):
    """One piece of justification for one fact.

    A fact may cite several; the truth of the fact is only as strong as the
    best of them, and a reader can always walk back to the event.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True, validate_default=True)

    id: str = ""
    fact_id: str = ""
    campaign_id: str = ""
    source_event_id: str = ""
    evidence_type: EvidenceType = EvidenceType.LLM_NARRATIVE
    observer_id: str = ""
    confidence: float = 0.5
    recorded_at: str = ""
    evidence_content: str = ""

    @field_validator("confidence")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


class MemoryEntity(BaseModel):
    """A person, place, organization or item, with every name it goes by."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    campaign_id: str
    canonical_name: str
    entity_type: str = "unknown"
    aliases: List[str] = Field(default_factory=list)
    description: str = ""
    created_at: str = ""
    updated_at: str = ""

    def all_names(self) -> List[str]:
        names = [self.canonical_name, *self.aliases]
        seen: List[str] = []
        for name in names:
            cleaned = str(name or "").strip()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    def matches(self, text: str) -> bool:
        """Whether any of this entity's names appears in `text`."""
        haystack = normalize_alias(text)
        return any(normalize_alias(name) in haystack for name in self.all_names() if name)


class MemoryRelation(BaseModel):
    """A typed edge between two entities, valid over a narrative window."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    campaign_id: str
    subject_id: str
    predicate: str
    object_id: str
    confidence: float = 0.5
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    source_event_ids: List[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))
