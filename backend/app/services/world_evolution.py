"""World evolution: the one place a player interaction may change the world (Phase 3).

The engine splits a hard line through this module:

* **Intent** is whatever the player typed. It is evidence, not truth.
* **Canon** is the authored world document. Only a validated proposal, and only
  the kinds marked canonical, may ever reach it.

So every evolution is a proposal with a normalized kind, a confidence, the
evidence it came from, and a status that says what happened to it. Proposals
are stored (``evolution_proposals``) before anything is committed, which is
what makes "the model made this up" a query rather than a story:

    GET /api/v1/worlds/{id}/evolution/proposals

The write path never runs an LLM. It re-classifies deterministic input with the
System Agent, stages authoring edits with ``authoring_session.stage_ops`` (which
re-validates the whole world document), and refuses anything that would
override established canon. Model involvement is explicit and separate:
``propose_edits`` for authoring ops and ``WorldEvolutionService.review`` for a
structural check of a pending proposal.

One thing this module deliberately does *not* do is write campaign state. NPC
beliefs and rumours belong to a character, not the world document, and the only
writer of campaign state is ``ActionService``.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.engines import system_agent as sa
from app.services import authoring_session

logger = logging.getLogger(__name__)
#: What a proposal is about, in the vocabulary shared by storage, the API and
#: the UI.
PROPOSAL_KINDS = (
    "character",
    "location",
    "item",
    "event",
    "relationship",
    "world_canon",
    "rumour",
    "secret",
    "player_backstory",
    "npc_belief",
    "quest",
    "storyline",
    "historical_fact",
)

#: Kinds that describe knowledge held by someone rather than truth about the
#: world. They are never written into the world document: canon would then
#: record a belief as a fact, which is the confusion this module prevents.
PERSONAL_KINDS = ("npc_belief", "rumour", "secret")

#: Proposal statuses. "pending" is the only one that can still be approved.
STATUS_PENDING = "pending"
STATUS_AUTO = "auto_committed"
STATUS_COMMITTED = "committed"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"

#: Above this confidence, and only for a canonical kind, a proposal may commit
#: on its own. Anything less waits for a human. This is a policy number, not a
#: model parameter: lowering it admits speculation into canon.
DEFAULT_AUTO_COMMIT = 0.75

#: The only kinds whose statement may become canon. Everything else is stored
#: and read back as context, but never written to the world document.
CANONICAL_PROPOSAL_KINDS = (
    "world_canon",
    "location",
    "item",
    "character",
    "event",
    "quest",
)

_KIND_FALLBACK = {
    "canonical_fact": "world_canon",
    "historical_fact": "historical_fact",
    "rumour": "rumour",
    "belief": "npc_belief",
    "hypothesis": "rumour",
    "character": "character",
    "location": "location",
    "item": "item",
    "event": "event",
    "relationship": "relationship",
    "player_backstory": "player_backstory",
}

#: Authoring op already implied by a proposal kind, so the evolution lane does
#: not need a model to re-plan what the classifier already decided.
_KIND_TO_OP = {
    "location": "add_location",
    "item": "add_item",
    "character": "add_entity",
    "event": "add_event",
    "quest": "add_quest",
}


def normalize_kind(raw: str) -> str:
    """Map a classifier kind onto a proposal kind, defaulting to unverified."""
    value = str(raw or "").strip().lower()
    if value in PROPOSAL_KINDS:
        return value
    return _KIND_FALLBACK.get(value, "rumour")


def is_canonical(kind: str) -> bool:
    return normalize_kind(kind) in CANONICAL_PROPOSAL_KINDS


def is_personal(kind: str) -> bool:
    """Whether this describes a character view rather than the world."""
    return normalize_kind(kind) in PERSONAL_KINDS

@dataclass
class EvolutionProposal:
    """One candidate change to the world, with the evidence behind it."""

    id: str
    kind: str
    statement: str
    confidence: float = 0.5
    world_id: str = ""
    campaign_id: str = ""
    origin: str = "player"
    source: str = ""
    observer: str = "player"
    status: str = STATUS_PENDING
    payload: Dict[str, Any] = field(default_factory=dict)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def canonical(self) -> bool:
        return is_canonical(self.kind)

    @property
    def personal(self) -> bool:
        return is_personal(self.kind)

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "world_id": self.world_id,
            "campaign_id": self.campaign_id,
            "kind": normalize_kind(self.kind),
            "status": self.status,
            "origin": self.origin,
            "statement": self.statement,
            "confidence": float(self.confidence),
            "source": self.source,
            "observer": self.observer,
            "payload": self.payload,
            "evidence": self.evidence,
            "warnings": self.warnings,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
        }

    def to_dict(self) -> Dict[str, Any]:
        data = self.to_row()
        data["canonical"] = self.canonical
        data["personal"] = self.personal
        return data

@dataclass
class EvolutionReport:
    """What one interaction did to the world, including what it refused."""

    world_id: str = ""
    campaign_id: str = ""
    proposals: List[EvolutionProposal] = field(default_factory=list)
    committed: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    degraded: bool = False
    error: str = ""

    @classmethod
    def empty(cls, world_id: str = "", campaign_id: str = "") -> """EvolutionReport""":
        return cls(world_id=world_id, campaign_id=campaign_id)

    @property
    def changed(self) -> bool:
        return bool(self.committed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "world_id": self.world_id,
            "campaign_id": self.campaign_id,
            "proposals": [p.to_dict() for p in self.proposals],
            "committed": list(self.committed),
            "pending": list(self.pending),
            "rejected": list(self.rejected),
            "conflicts": list(self.conflicts),
            "warnings": list(self.warnings),
            "degraded": self.degraded,
            "error": self.error,
        }



def _carried(proposal: EvolutionProposal, applied: Sequence[Any]) -> bool:
    """Whether this claim actually reached the staged draft.

    stage_ops returns plain dicts of kind/summary, not references to the
    proposals, so the claim name is what we can match on.
    """
    name = _claim_name(proposal)
    if not name:
        return False
    wanted = " ".join(name.split()).lower()
    op = str(proposal.payload.get("op") or "")
    for item in applied or ():
        if not isinstance(item, dict):
            continue
        if op and str(item.get("kind") or "") != op:
            continue
        summary = item.get("summary") or ""
        if wanted and wanted in " ".join(str(summary).split()).lower():
            return True
    return False

def proposal_id(world_id: str, statement: str, kind: str = "") -> str:
    """Stable id, so the same claim twice does not become two proposals."""
    seed = "{0}|{1}|{2}".format(world_id or "", normalize_kind(kind), " ".join(str(statement or "").split()).lower())
    return "evo_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _slug(name: str, prefix: str) -> str:
    """A stable, readable id derived from the name."""
    ascii_name = unicodedata.normalize("NFKD", str(name or ""))
    ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", ascii_name) if w]
    stem = "_".join(words)[:40].strip("_")
    if not stem:
        stem = hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:8]
    return "{0}_{1}".format(prefix, stem)

def _op_for(kind: str) -> str:
    return _KIND_TO_OP.get(normalize_kind(kind), "")


def _entity_payload(
    name: str, description: str, location_id: str = "", entity_id: str = ""
) -> Dict[str, Any]:
    """Shape an EntityDef payload for a character the player introduced."""
    payload: Dict[str, Any] = {
        "id": entity_id or _slug(name, "npc"),
        "name": name.strip(),
        "kind": "npc",
        "description": description.strip() or name.strip(),
        "location_id": location_id,
    }
    return payload


def _location_payload(name: str, description: str, location_id: str = "") -> Dict[str, Any]:
    return {
        "id": location_id or _slug(name, "loc"),
        "name": name.strip(),
        "description": description.strip() or name.strip(),
    }


def _item_payload(name: str, description: str, item_id: str = "") -> Dict[str, Any]:
    return {
        "id": item_id or _slug(name, "item"),
        "name": name.strip(),
        "description": description.strip() or name.strip(),
    }

def statements_to_proposals(
    statements: Sequence[Any],
    *,
    world_id: str = "",
    campaign_id: str = "",
    origin: str = "player",
    source: str = "",
    observer: str = "player",
    known: Sequence[str] = (),
) -> Tuple[List[EvolutionProposal], List[str]]:
    """Turn classified statements into proposals without writing anything."""
    proposals: List[EvolutionProposal] = []
    warnings: List[str] = []
    seen: Dict[str, EvolutionProposal] = {}
    catalogue = [
        " ".join(str(item).split()).lower()
        for item in known
        if str(item).strip()
    ]
    for statement in statements:
        text = str(getattr(statement, "text", "") or "").strip()
        if not text:
            continue
        kind = normalize_kind(getattr(statement, "kind", ""))
        confidence = _clamp(getattr(statement, "confidence", 0.5))
        entities = [str(e) for e in (getattr(statement, "entities", None) or [])]
        payload_name = (_proper_name(text) if _claim_name_for(kind) else "") or " ".join(
            text.rstrip(".").split()
        )[:60]
        payload_name = _trim_label(payload_name)
        known_already = _already_exists(entities, text, catalogue, kind=kind)
        if known_already:
            warnings.append("already in the world, kept as a reference: " + text)
        pid = proposal_id(world_id, text, kind)
        proposal = EvolutionProposal(
            id=pid,
            kind=kind,
            statement=text,
            confidence=confidence,
            world_id=world_id,
            campaign_id=campaign_id,
            origin=origin,
            source=source or "interaction",
            observer=observer,
            payload=_claim_payload(kind, payload_name, entities, text=text),
            evidence=[{"text": text, "kind": getattr(statement, "kind", kind)}],
        )
        if pid in seen:
            warnings.append("duplicate claim inside one turn: " + text)
            continue
        if known_already:
            proposal.payload["duplicate_of"] = payload_name.lower()
        seen[pid] = proposal
        proposals.append(proposal)
    return proposals, warnings

#: Leading words that never start a proper name, so a sentence can be mined
#: for one without a model.
_STOPWORDS = frozenset(
    """
    a an the this that these those there here it its my our your their his her
    i we you he she they me us them some any old new and or but if when while
    in on at to from of for with by about into over under near across
    """.split()
)


def _proper_name(text: str) -> str:
    """Pull the most name-like capitalised run out of a sentence."""
    words = str(text or "").split()
    run: List[str] = []
    best: List[str] = []
    for raw in words:
        word = raw.strip(".,;:!?\"'()[]")
        bare = word.lower()
        starts_capital = bool(word) and word[0].isupper()
        if starts_capital and bare not in _STOPWORDS:
            run.append(word)
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    if len(best) >= 2:
        return " ".join(best)
    return ""


def _trim_label(name: str) -> str:
    """A short noun phrase from a sentence, used only as a label."""
    words = [w.strip(".,;:!?\"'()[]") for w in str(name or "").split()]
    kept: List[str] = []
    for word in words:
        if not word:
            continue
        if kept and word.lower() in _STOPWORDS and len(" ".join(kept)) > 12:
            break
        kept.append(word)
        if len(kept) >= 5:
            break
    return " ".join(kept).strip()


def _claim_name_for(kind: str) -> bool:
    return normalize_kind(kind) in ("location", "item", "character", "quest")


def _claim_payload(
    kind: str,
    name: str,
    entities: Sequence[str],
    *,
    text: str = "",
) -> Dict[str, Any]:
    """What the staging lane needs in order to build a real definition."""
    payload: Dict[str, Any] = {
        "entities": list(entities),
        "op": _op_for(kind),
    }
    if name:
        payload["name"] = name
    return payload

def _claim_name(proposal: EvolutionProposal) -> str:
    """What a proposal would call the thing it introduces."""
    payload = proposal.payload or {}
    name = str(payload.get("name") or "").strip()
    if name:
        return name
    for entity in payload.get("entities") or ():
        if str(entity).strip():
            return str(entity).strip()
    return ""


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, number))


def _already_exists(
    entities: Sequence[str],
    text: str,
    catalogue: Sequence[str],
    *,
    kind: str = "",
) -> bool:
    """Entity resolution: is this claim about something the world already has?"""
    lowered = " ".join(str(text or "").split()).lower()
    # A name merely mentioned inside a longer sentence is a new claim about a
    # known thing: the conflict check decides what to do with it, not dedupe.
    if kind and lowered in catalogue:
        return False
    for entity in entities:
        key = " ".join(str(entity).split()).lower()
        if key and key in catalogue:
            return False
    return False


def _canon_conflict(
    proposal: EvolutionProposal,
    world: Any,
) -> str:
    """Refuse a canonical proposal that would silently replace known text."""
    if not proposal.canonical:
        return ""
    kind = normalize_kind(proposal.kind)
    wanted = " ".join(str(proposal.payload.get("name") or "").split()).lower()
    if not wanted:
        return ""
    existing = _existing_names(world, kind)
    if wanted and wanted in existing:
        return "the world already defines {0} {1!r}; edit it instead of re-declaring it".format(kind, wanted)
    # A claim that names a place the world already has is a claim *about* that
    # place, not a second place with the same name. Longest match wins, so
    # "Old Chapel" is preferred over "Chapel".
    span = " ".join(str(proposal.statement or "").split()).lower()
    for name in sorted(existing, key=len, reverse=True):
        if len(name) >= 3 and name in span:
            return "the world already defines {0} {1!r}; edit it instead of re-declaring it".format(kind, name)
    return ""


def _existing_names(world: Any, kind: str) -> List[str]:
    if world is None:
        return []
    if kind == "location":
        source = getattr(world, "locations", {}) or {}
    elif kind == "item":
        source = getattr(world, "items", {}) or {}
    elif kind == "character":
        source = getattr(world, "entities", {}) or {}
    elif kind == "quest":
        source = getattr(world, "quests", {}) or {}
    elif kind == "event":
        source = getattr(world, "events", {}) or {}
    else:
        return []
    names: List[str] = []
    try:
        values = source.values() if hasattr(source, "values") else source
    except Exception:
        return []
    for value in values:
        name = getattr(value, "name", "") or ""
        if name:
            names.append(" ".join(str(name).split()).lower())
    return names

class WorldEvolutionService:
    """Decide what a player interaction may change, and record every decision."""

    def __init__(
        self,
        store: Any = None,
        *,
        scenario_store: Any = None,
        system: Any = None,
        auto_commit: float = DEFAULT_AUTO_COMMIT,
        language: str = "en",
        persist: Any = None,
    ) -> None:
        self.persist = persist
        self.store = store
        self.scenario_store = scenario_store
        self.system = system
        self.auto_commit = _clamp(auto_commit)
        self.language = language

    def _known(self, world: Any, knowledge: Optional[Sequence[Any]] = None) -> List[str]:
        names: List[str] = []
        for kind in ("location", "item", "character", "quest", "event"):
            names.extend(_existing_names(world, kind))
        for entry in knowledge or ():
            title = getattr(entry, "title", "") or ""
            if title:
                names.append(" ".join(str(title).split()).lower())
        return names

    def propose(
        self,
        world: Any,
        statements: Sequence[Any],
        *,
        campaign_id: str = "",
        origin: str = "player",
        source: str = "",
        observer: str = "player",
        knowledge: Optional[Sequence[Any]] = None,
    ) -> EvolutionReport:
        """Build the proposal list for one turn. Nothing is committed here."""
        world_id = getattr(world, "world_id", "") or ""
        report = EvolutionReport.empty(world_id=world_id, campaign_id=campaign_id)
        known = self._known(world, knowledge)
        proposals, warnings = statements_to_proposals(
            statements,
            world_id=world_id,
            campaign_id=campaign_id,
            origin=origin,
            source=source,
            observer=observer,
            known=known,
        )
        report.warnings.extend(warnings)
        for proposal in proposals:
            problem = _canon_conflict(proposal, world)
            if problem:
                proposal.status = STATUS_REJECTED
                proposal.error = problem
                report.rejected.append(proposal.id)
                report.conflicts.append(problem)
            report.proposals.append(proposal)
        return report
    def _stage(
        self,
        world: Any,
        proposals: Sequence[EvolutionProposal],
    ) -> Tuple[Any, List[Dict[str, Any]], List[str]]:
        """Run the canonical proposals through the authoring validator."""
        ops: List[Dict[str, Any]] = []
        for proposal in proposals:
            op = str(proposal.payload.get("op") or _op_for(proposal.kind) or "")
            if not op:
                continue
            data = dict(proposal.payload)
            data.pop("op", None)
            data.pop("entities", None)
            label = str(data.get("name") or _claim_name(proposal) or "")
            if op == "add_location":
                data = _location_payload(
                    label, data.get("description", ""), data.get("id", "")
                )
            elif op == "add_item":
                data = _item_payload(
                    label, data.get("description", ""), data.get("id", "")
                )
            elif op == "add_entity":
                data = _entity_payload(
                    label,
                    data.get("description", ""),
                    str(data.get("location_id") or ""),
                    data.get("id", ""),
                )
            ops.append({"kind": op, "data": data})
        if not ops:
            return world, [], []
        try:
            draft, applied, warnings = authoring_session.stage_ops(world, ops)
        except Exception as exc:
            logger.warning("world evolution staging failed: %s", exc)
            return world, [], ["staging failed: {0}: {1}".format(type(exc).__name__, exc)]
        return draft, applied, list(warnings)
    def commit(
        self,
        report: EvolutionReport,
        world: Any,
        *,
        draft: Any = None,
        applied: Optional[Sequence[Any]] = None,
    ) -> EvolutionReport:
        """Persist the world draft, but only if every reference still resolves."""
        if draft is None or draft is world:
            return report
        checker = getattr(draft, "validate_references", None)
        if callable(checker):
            problems = checker()
            if problems:
                report.rejected.extend(report.pending)
                report.pending = []
                report.conflicts.extend(str(p) for p in problems)
                report.error = "draft does not validate"
                return report
        if self.persist is not None:
            save = self.persist
        else:
            save = getattr(self.scenario_store, "save_world", None)
        if not callable(save):
            report.error = "no scenario store to commit into"
            return report
        try:
            save(draft)
        except Exception as exc:
            report.error = "{0}: {1}".format(type(exc).__name__, exc)
            logger.warning("world evolution commit failed: %s", exc)
            return report
        # Optimistic when the caller did not say what was staged; strict when it did,
        # so a claim that never reached the draft is not reported as committed.
        for proposal in report.proposals:
            if proposal.status != STATUS_PENDING or not proposal.canonical or not proposal.id:
                continue
            if applied is not None and not _carried(proposal, applied):
                continue
            proposal.status = STATUS_AUTO
            report.committed.append(proposal.id)
        return report

    def _eligible(self, proposal: EvolutionProposal) -> bool:
        return (
            proposal.status == STATUS_PENDING
            and proposal.canonical
            and proposal.confidence >= self.auto_commit
            and bool(_claim_name(proposal) or proposal.payload.get("op") is None)
        )

    def record(self, report: EvolutionReport) -> EvolutionReport:
        """Persist the audit trail. A telemetry failure must not lose the turn."""
        recorder = getattr(self.store, "record_proposal", None)
        if not callable(recorder):
            return report
        for proposal in report.proposals:
            try:
                recorder(proposal)
            except Exception as exc:
                logger.warning("could not record proposal %s: %s", proposal.id, exc)
                report.warnings.append("proposal not recorded: {0}".format(proposal.id))
        return report

    async def review(self, proposal: EvolutionProposal, *, world: Any = None) -> EvolutionProposal:
        """Ask the System Agent to check a pending proposal. Never commits."""
        if self.system is None or proposal.status != STATUS_PENDING:
            return proposal
        try:
            verdict = await self.system.review_ops(
                proposal.statement,
                known=self._known(world, None) if world is not None else (),
            )
        except Exception as exc:
            proposal.warnings.append("review failed: {0}".format(exc))
            return proposal
        if verdict is not None:
            proposal.result["review"] = verdict
        return proposal

    def run(
        self,
        world: Any,
        statements: Sequence[Any],
        *,
        campaign_id: str = "",
        origin: str = "player",
        source: str = "",
        observer: str = "player",
        knowledge: Optional[Sequence[Any]] = None,
        commit: bool = False,
    ) -> EvolutionReport:
        """The whole lane for one turn: propose, validate, maybe commit, record."""
        report = self.propose(
            world,
            statements,
            campaign_id=campaign_id,
            origin=origin,
            source=source,
            observer=observer,
            knowledge=knowledge,
        )
        eligible = [p for p in report.proposals if self._eligible(p)]
        for proposal in report.proposals:
            if proposal.status == STATUS_PENDING and proposal.id not in {p.id for p in eligible}:
                report.pending.append(proposal.id)
        if eligible and commit:
            draft, applied, warnings = self._stage(world, eligible)
            report.warnings.extend(warnings)
            report = self.commit(report, world, draft=draft, applied=applied)
            for proposal in eligible:
                if proposal.status == STATUS_PENDING and not _carried(proposal, applied):
                    proposal.status = STATUS_REJECTED
                    proposal.error = "the claim could not be staged: " + (
                        report.warnings[0] if report.warnings else "no op was applied"
                    )
                    report.rejected.append(proposal.id)
        self.record(report)
        return report


    def commit_pending(self, world: Any, proposal: EvolutionProposal) -> EvolutionReport:
        """Commit one human-approved proposal, re-validating the world draft."""
        report = EvolutionReport.empty(
            world_id=getattr(world, "world_id", "") or proposal.world_id,
            campaign_id=proposal.campaign_id,
        )
        proposal.status = STATUS_PENDING
        report.proposals.append(proposal)
        if not self._eligible(proposal):
            why = "not committable: kind={0} confidence={1}".format(
                proposal.kind, round(proposal.confidence, 2)
            )
            proposal.status = STATUS_REJECTED
            proposal.error = why
            report.rejected.append(proposal.id)
            self.record(report)
            return report
        draft, applied, warnings = self._stage(world, [proposal])
        report.warnings.extend(warnings)
        if not applied:
            proposal.status = STATUS_REJECTED
            proposal.error = warnings[0] if warnings else "draft did not change"
            report.rejected.append(proposal.id)
            self.record(report)
            return report
        report = self.commit(report, world, draft=draft, applied=applied)
        if proposal.status == STATUS_AUTO:
            proposal.status = STATUS_COMMITTED
        self.record(report)
        return report

def build_evolution_service(
    store: Any = None,
    *,
    scenario_store: Any = None,
    system: Any = None,
    language: str = "en",
    persist: Any = None,
) -> WorldEvolutionService:
    return WorldEvolutionService(
        store,
        scenario_store=scenario_store,
        system=system,
        language=language,
        persist=persist,
    )
