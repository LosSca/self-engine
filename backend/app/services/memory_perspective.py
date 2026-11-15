from __future__ import annotations

"""Who is allowed to know what (P3).

Three different things get called "what happened" in this codebase, and the
memory system is only honest if it keeps them apart:

    WORLD TRUTH      the canonical record the engine committed
    NPC OBSERVATION  the events that NPC actually witnessed
    NPC INFERENCE    what that NPC believes, which may be partial or wrong

The witnessed_by mechanism already delivers observation isolation, and it
fails closed. What it cannot express is the campaign where two characters are
told different stories: Ellen's player knows Eileen left on a secret errand,
the duke believes she is dead, and the villain knows the errand's real target.
Those are not different observations of one event - they are different claims
about it, some of them wrong on purpose.

An NpcPerspective (or a ClaimView, which is the same thing attached to a fact)
is the smallest structure that carries all three without letting the wrong one
be promoted: it always shows the truth *to the engine*, marks which part of it
the NPC saw, and keeps an explicit, signed-off divergence for that NPC alone.
Filtering happens here, before any prompt is built, so a leak would require
disabling code rather than forgetting an instruction.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from app.models.memory import EvidenceType, FactStatus, MemoryFact, normalize_alias

logger = logging.getLogger(__name__)


#: The three knowledge levels a character can have about an event or a fact.
KNOWS = "knows"            # witnessed it, or was told by an authoritative source
BELIEVES = "believes"      # has heard a version of it, possibly a wrong one
UNAWARE = "unaware"        # has no claim at all - the default, never shown


def _key(name) -> str:
    return normalize_alias(name)


def _keys(names) -> List[str]:
    if not names:
        return []
    if isinstance(names, str):
        names = [names]
    out: List[str] = []
    for name in names:
        key = _key(name)
        if key and key not in out:
            out.append(key)
    return out

@dataclass(frozen=True)
class Claim:
    """One fact as a particular character understands it.

    `subject`/`predicate`/`object_value` mirror MemoryFact, so a claim can be
    compared with the world fact it is about; `stance` says whether the
    character holds the fact as true or holds a *different* object_value for
    it. A belief that contradicts the world is exactly the interesting case:
    the duke is certain Eileen is dead, and he is wrong, and both halves of
    that sentence have to survive into the prompt.
    """

    subject_id: str
    predicate: str
    object_value: str
    stance: str = KNOWS
    confidence: float = 0.5
    source_fact_id: str = ""
    note: str = ""

    def key(self) -> tuple:
        return (_key(self.subject_id), _key(self.predicate))

    def contradicts(self, fact: "MemoryFact | Claim | str") -> bool:
        """Whether this claim names a different value for the same slot."""
        other = getattr(fact, "object_value", fact)
        mine = _key(self.object_value)
        theirs = _key(other)
        return bool(mine and theirs and mine != theirs)

    def render(self) -> str:
        prefix = "knows" if self.stance == KNOWS else "believes"
        text = f"{prefix}: {self.subject_id} {self.predicate} {self.object_value}"
        if self.note:
            text += f" ({self.note})"
        return text


@dataclass
class NpcPerspective:
    """What one character knows, believes and has never heard of.

    Build it from the campaign (build_perspective) rather than by hand: the
    point of this type is that the three levels are *derived* from evidence
    instead of typed in as prose, so a narrator cannot quietly promote a
    rumour.
    """

    npc_id: str
    entity_id: str = ""
    claims: List[Claim] = field(default_factory=list)
    #: Facts this character is allowed to see, already permission-filtered.
    known_fact_ids: List[str] = field(default_factory=list)
    #: Fact ids whose world value this character would get wrong.
    mistaken_fact_ids: List[str] = field(default_factory=list)
    #: Events this character witnessed (raw event ids).
    witnessed_event_ids: List[str] = field(default_factory=list)

    def _claim_map(self) -> Dict[tuple, Claim]:
        return {claim.key(): claim for claim in self.claims}

    def claim_about(self, subject_id: str, predicate: str) -> Optional[Claim]:
        return self._claim_map().get((_key(subject_id), _key(predicate)))

    def believes(self, fact: MemoryFact) -> Optional[Claim]:
        """This character's own version of `fact`, when it differs from the world."""
        claim = self.claim_about(fact.subject_id, fact.predicate)
        if claim is None:
            return None
        return claim if claim.contradicts(fact) else None

    def knows(self, fact: MemoryFact) -> bool:
        """Whether the character may be told this fact at all."""
        if fact.id and fact.id in self.known_fact_ids:
            return True
        return self.claim_about(fact.subject_id, fact.predicate) is not None

    def visible_facts(self, facts: Iterable[MemoryFact]) -> List[MemoryFact]:
        """Facts this character may see, with their own belief applied."""
        out: List[MemoryFact] = []
        for fact in facts or ():
            if self.knows(fact):
                out.append(fact)
        return out

    def render(self, limit: int = 12) -> str:
        """The NPC's side of the prompt: its own claims, labelled as such."""
        if not self.claims:
            return ""
        lines = [f"=== {self.npc_id}: what they know and believe ==="]
        for claim in self.claims[:limit]:
            lines.append("- " + claim.render())
        return "\n".join(lines)

def build_perspective(
    npc_id: str,
    *,
    facts: Sequence[MemoryFact] = (),
    observations: Iterable[str] = (),
    beliefs: Sequence[Claim] = (),
    entity_id: str = "",
) -> NpcPerspective:
    """Derive a perspective from evidence instead of prose.

    `facts` are the world facts that were already permission-filtered for this
    character (observer_ids contains them, or the fact is public). `beliefs`
    are the divergent claims - what this character was told and holds true;
    they are attached by slot, so a belief about Eileen's status does not
    smuggle in a belief about anything else.

    A belief whose value equals the world value is downgraded to KNOWS: we do
    not want a prompt that says "believes X" when the character simply knows
    X, because that invites the narrator to hash out a dispute that is not
    there.
    """
    world = {(_key(f.subject_id), _key(f.predicate)): f for f in facts or ()}
    claims: List[Claim] = []
    seen: Dict[tuple, Claim] = {}
    for claim in beliefs or ():
        slot = claim.key()
        fact = world.get(slot)
        if fact is not None and not claim.contradicts(fact):
            normalised = Claim(
                subject_id=fact.subject_id,
                predicate=fact.predicate,
                object_value=fact.object_value,
                stance=KNOWS,
                confidence=claim.confidence,
                source_fact_id=fact.id or claim.source_fact_id,
                note=claim.note,
            )
            claims.append(normalised)
            seen[slot] = normalised
            continue
        seen.setdefault(slot, claim)
        claims.append(claim)

    perspective = NpcPerspective(
        npc_id=npc_id,
        entity_id=entity_id or _key(npc_id),
        claims=claims,
        witnessed_event_ids=list(observations or ()),
    )
    for fact in facts or ():
        if fact.id:
            perspective.known_fact_ids.append(fact.id)
            if (_key(fact.subject_id), _key(fact.predicate)) in seen:
                belief = seen[(_key(fact.subject_id), _key(fact.predicate))]
                if belief.contradicts(fact):
                    perspective.mistaken_fact_ids.append(fact.id)
    perspective.known_fact_ids.sort()
    perspective.mistaken_fact_ids.sort()
    return perspective


#: Thought-key prefix for a claim a character was told. Kept here so the
#: reader (MemoryEngine.stored_beliefs) and the writer share one definition.
BELIEF_KEY_PREFIX = "belief:"


def belief_key(subject_id: str, predicate: str) -> str:
    """The thought key that stores "someone told this character X is Y".

    Encoding is belief:<predicate>:<subject> - three structural fields, so
    both parts stay readable in a debug panel and neither needs escaping as
    long as they contain no colon. A colon is replaced rather than rejected:
    losing a separator beats losing the claim.
    """
    subject = str(subject_id or "").strip().replace(":", " ")
    predicate_clean = str(predicate or "").strip().replace(":", " ")
    if not (subject and predicate_clean):
        return ""
    return f"{BELIEF_KEY_PREFIX}{predicate_clean}:{subject}"


def remember_belief(
    minds,
    campaign_id: str,
    npc_name: str,
    *,
    subject_id: str,
    predicate: str,
    object_value: str,
    turn: int = 0,
) -> str:
    """Record that npc_name was told subject/predicate/object.

    Returns the stored key, or an empty string when nothing was written.
    Deliberately permissive: rumour is the normal way a character learns
    something in an RPG, and refusing to store a claim the engine cannot
    verify is exactly backwards - the *claim* is the data, and verification
    happens later when it is compared with the world fact.

    Beliefs never decay: a character does not forget what they were told
    because turns passed, and "the duke still thinks she is dead" is the
    whole point of keeping the divergence.
    """
    key = belief_key(subject_id, predicate)
    value = str(object_value or "").strip()
    if not key or not value or minds is None:
        return ""
    try:
        mind = minds.ensure_mind(campaign_id, npc_name, current_turn=turn)
        mind.set_thought(key, value, current_turn=turn, decay_after_turns=0)
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        logger.warning("could not record a belief for %s: %s", npc_name, exc)
        return ""
    return key


def filter_facts_for_observer(facts: Iterable[MemoryFact], observer: str) -> List[MemoryFact]:
    """Keep facts this observer may see, before anything is built from them.

    Public facts (no observers recorded) are visible to everyone, which is a
    deliberate legacy-compatible reading: a fact written before observer
    tracking existed must not vanish from every prompt. Facts with observers
    are visible only to the listed ones - the gallery gossips, but not to the
    villain.
    """
    key = _key(observer)
    if not key:
        return list(facts or ())
    out: List[MemoryFact] = []
    for fact in facts or ():
        observers = _keys(getattr(fact, "observer_ids", ()))
        if not observers or key in observers:
            out.append(fact)
    return out


RETRACTED_STATUSES = frozenset({FactStatus.RETRACTED.value, FactStatus.REJECTED.value})


def perspective_fact_ids(perspective: Optional[NpcPerspective]) -> set:
    """Fact ids an NPC may see; empty set when there is no perspective."""
    if perspective is None:
        return set()
    return set(perspective.known_fact_ids)

def _status_of(fact: MemoryFact) -> str:
    return str(getattr(fact.status, "value", fact.status) or "")


def facts_for_prompt(
    facts: Sequence[MemoryFact],
    *,
    observer: str = "",
    perspective: Optional[NpcPerspective] = None,
    limit: int = 20,
) -> List[str]:
    """Render the fact block of a prompt, filtered and labelled by knowledge level.

    Returns prompt lines (not facts) on purpose: the caller must not be able
    to reuse an unfiltered list after this point. Every line names its status,
    because "world fact" and "what this NPC believes" must not look alike in
    a prompt.
    """
    allowed = filter_facts_for_observer(facts or (), observer) if observer else list(facts or ())
    allowed = [f for f in allowed if _status_of(f) not in RETRACTED_STATUSES]
    if perspective is not None:
        known = perspective_fact_ids(perspective)
        if known:
            allowed = [f for f in allowed if f.id in known]

    lines: List[str] = []
    for fact in allowed[:limit]:
        belief = perspective.believes(fact) if perspective is not None else None
        if belief is not None:
            lines.append(
                f"- (heard, may be wrong) {belief.subject_id} {belief.predicate} "
                f"{belief.object_value}"
            )
            continue
        status = _status_of(fact)
        label = "fact" if status in (FactStatus.ACTIVE.value, FactStatus.VERIFIED.value) else "unconfirmed"
        lines.append(f"- ({label}) {fact.subject_id} {fact.predicate} {fact.object_value}")
    return lines