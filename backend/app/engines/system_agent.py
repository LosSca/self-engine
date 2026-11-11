"""The System Agent: classification and structured review, never authority.

The engine keeps deterministic control of world state (see ``ActionService``).
The System Agent only *proposes*: it reads a piece of player-authored text and
returns a structured, validated plan of what it might mean. Nothing here writes
state; callers turn a plan into proposals through the existing authoring and
knowledge paths, which validate again before anything lands.

Two rules shape the design:

* Cheap classification is local. A deterministic classifier runs first and
  answers the common case, so ordinary play never pays for a flagship model.
* Only conflict detection and plans that would touch canon reach the model, and
  its answer is parsed into typed payloads that are dropped - with a warning -
  when they do not validate. Unknown kinds never reach a proposal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from app.engines.agent_router import AgentReply, AgentRuntime, extract_json_object
from app.services.knowledge_extractor import classify_sentence, split_sentences

logger = logging.getLogger(__name__)

#: What a statement is, in the engine's terms. The first set carries the weight:
#: a canonical fact is world truth, a historical fact already happened, a rumour
#: is unverified talk, and a belief belongs to one character only.
INFO_KINDS = (
    "canonical_fact",
    "historical_fact",
    "rumour",
    "belief",
    "hypothesis",
    "character",
    "location",
    "item",
    "event",
    "relationship",
    "player_backstory",
)

#: Kinds that may be written straight into world canon by a deterministic path.
CANON_KINDS = ("canonical_fact",)

#: Kinds that must never be promoted to canon without explicit author approval:
#: someone said it, somebody believes it, or a model guessed it.
UNVERIFIED_KINDS = ("rumour", "belief", "hypothesis")

#: Authoring operations the System Agent may propose. Mirrors
#: ``authoring_session.OP_KINDS`` but resolved locally so a plan can be built
#: without importing the whole LLM authoring module.
OP_KINDS = (
    "add_location",
    "add_item",
    "add_door",
    "add_entity",
    "add_event",
    "add_quest",
    "add_knowledge",
    "set_note",
)

#: Words that mark unverified information, checked before anything else.
_RUMOUR_MARKERS = (
    "rumour", "rumor", "rumoured", "rumored", "they say", "i heard", "heard that",
    "gossip", "supposedly", "allegedly", "is said to",
    "传闻", "听说", "据说", "传言", "流言",
)
_BELIEF_MARKERS = (
    "believes", "thinks that", "is convinced", "claims", "insists", "assumes",
    "认为", "相信", "确信", "自认为", "坚持说", "以为",
)
_HYPOTHESIS_MARKERS = (
    "perhaps", "maybe", "might", "could be", "possibly", "probably", "seems likely",
    "也许", "可能", "或许", "大概", "不确定",
)
_BACKSTORY_MARKERS = (
    "when i was a child", "as a child", "grew up", "my hometown", "back home",
    "小时候", "童年", "我从小", "家乡",
    "居住过", "生活过的",
)

#: Sentence shapes that name a place or an object. Kept deliberately narrow: a
#: wrong extraction is worse than a missed one, because the caller turns these
#: into authoring proposals.
_LOCATION_HINTS = (
    "monastery", "village", "forest", "warehouse", "tower", "temple", "ruin",
    "修道院", "村庄", "森林", "仓库",
    "城堡", "神殿", "遗迹", "山谷", "港口",
)
_ITEM_HINTS = (
    "key", "ring", "amulet", "letter", "sword", "blade", "potion", "map", "ledger",
    "钥匙", "戒指", "护身符", "信件",
    "剑", "药水", "地图", "账本",
)

SYSTEM_PROMPT = (
    "You classify statements a player makes about a text RPG world.\n"
    "You never change the game. You only return JSON.\n"
    "For every statement decide what it is, using exactly these kinds: "
    + ", ".join(INFO_KINDS)
    + ".\n"
    "Rules:\n"
    "- A canonical_fact is world truth; a historical_fact already happened.\n"
    "- Anything somebody merely said, heard or believes is a rumour or a belief,"
    " never a canonical_fact.\n"
    "- Anything you are guessing is a hypothesis.\n"
    "- Mark a statement that contradicts the established facts with",
    " conflict=true and name the fact it contradicts.\n"
    'Return {"statements": [{"text": str, "kind": str, "confidence": 0..1,'
    ' "entities": [str], "conflict": bool, "conflicts_with": str}]}.'
)


@dataclass
class Statement:
    """One classified piece of player text."""

    text: str
    kind: str = "canonical_fact"
    confidence: float = 0.5
    entities: List[str] = field(default_factory=list)
    conflict: bool = False
    conflicts_with: str = ""
    origin: str = "rules"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "confidence": round(float(self.confidence), 3),
            "entities": list(self.entities),
            "conflict": self.conflict,
            "conflicts_with": self.conflicts_with,
            "origin": self.origin,
        }


@dataclass
class Classification:
    """What the System Agent made of one player message."""

    text: str
    statements: List[Statement] = field(default_factory=list)
    ops: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    consulted_agent: bool = False
    degraded: bool = False
    error: str = ""

    @property
    def empty(self) -> bool:
        return not self.statements and not self.ops

    def kinds(self) -> List[str]:
        seen: List[str] = []
        for statement in self.statements:
            if statement.kind not in seen:
                seen.append(statement.kind)
        return seen

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "statements": [s.to_dict() for s in self.statements],
            "ops": list(self.ops),
            "conflicts": list(self.conflicts),
            "warnings": list(self.warnings),
            "consulted_agent": self.consulted_agent,
            "degraded": self.degraded,
            "error": self.error,
        }


# ------------------------------------------------------------------ rules
def _has_any(text: str, markers: Sequence[str]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def classify_text(text: str) -> Optional[str]:
    """Classify one sentence by shape alone.

    Order matters: unverified markers win over everything, then backstory,
    then places and objects, and only then the generic fact/event split that
    ``knowledge_extractor`` already uses for narration.
    """
    sentence = (text or "").strip()
    if not sentence:
        return None
    # A question asserts nothing, so it can never become a fact - not even a
    # place or an object mentioned in passing.
    if sentence.endswith(("?", "？", "。？")):
        return None
    if _has_any(sentence, _RUMOUR_MARKERS):
        return "rumour"
    if _has_any(sentence, _HYPOTHESIS_MARKERS):
        return "hypothesis"
    if _has_any(sentence, _BELIEF_MARKERS):
        return "belief"
    if _has_any(sentence, _BACKSTORY_MARKERS):
        return "player_backstory"
    if _has_any(sentence, _LOCATION_HINTS):
        return "location"
    if _has_any(sentence, _ITEM_HINTS):
        return "item"
    layer = classify_sentence(sentence)
    if layer == "character":
        return "character"
    if layer == "event":
        return "event"
    return None


def initial_confidence(kind: str) -> float:
    """How much the engine trusts a *rule-classified* statement."""
    return {
        "canonical_fact": 0.7,
        "historical_fact": 0.8,
        "character": 0.6,
        "location": 0.6,
        "item": 0.6,
        "event": 0.55,
        "relationship": 0.5,
        "player_backstory": 0.6,
        "belief": 0.4,
        "rumour": 0.3,
        "hypothesis": 0.2,
    }.get(kind, 0.5)


def needs_agent(statements: Sequence[Statement]) -> bool:
    """Whether a message deserves a model call at all.

    The common case is deterministic. A model is consulted only when the
    classification is genuinely uncertain: an unverified statement that might
    matter, or a sentence the rules could not place.
    """
    for statement in statements:
        if statement.kind in UNVERIFIED_KINDS and len(statement.text) > 24:
            return True
    return False


def classify_locally(text: str) -> Classification:
    """Deterministic pass: one statement per sentence, no model, no state."""
    result = Classification(text=text)
    for sentence in split_sentences(text):
        kind = classify_text(sentence)
        if not kind:
            continue
        result.statements.append(
            Statement(
                text=sentence,
                kind=kind,
                confidence=initial_confidence(kind),
                entities=[sentence],
            )
        )
    return result


# ------------------------------------------------------------ model pass
def _coerce_kind(raw: Any) -> str:
    value = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    return value if value in INFO_KINDS else ""


def _coerce_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, value))


def parse_agent_payload(payload: Optional[Dict[str, Any]]) -> List[Statement]:
    """Turn a model answer into validated statements.

    Anything that does not validate is dropped and reported by the caller as a
    warning; an unknown kind never becomes a statement, because the write path
    decides what a kind is allowed to do.
    """
    if not isinstance(payload, dict):
        return []
    items = payload.get("statements")
    if not isinstance(items, list):
        return []
    out: List[Statement] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sentence = str(item.get("text") or "").strip()
        kind = _coerce_kind(item.get("kind"))
        if not sentence or not kind:
            continue
        entities = item.get("entities")
        if not isinstance(entities, list):
            entities = []
        out.append(
            Statement(
                text=sentence,
                kind=kind,
                confidence=_coerce_float(item.get("confidence"), initial_confidence(kind)),
                entities=[str(e).strip() for e in entities if str(e).strip()],
                conflict=bool(item.get("conflict")),
                conflicts_with=str(item.get("conflicts_with") or "").strip(),
                origin="agent",
            )
        )
    return out


def _agent_messages(text: str, known: Sequence[str], language: str) -> List[Dict[str, str]]:
    known_text = "\n".join(f"- {line}" for line in list(known)[:24]) or "(none recorded)"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Language: {language}\n"
                f"Established world facts:\n{known_text}\n\n"
                f"Player text:\n{text}"
            ),
        },
    ]


class SystemAgent:
    """Classification and structured review, bounded by validation."""

    def __init__(
        self,
        agents: Optional[AgentRuntime] = None,
        *,
        language: str = "en",
        min_confidence: float = 0.35,
    ):
        self.agents = agents
        self.language = language
        self.min_confidence = float(min_confidence)

    async def classify(
        self,
        text: str,
        *,
        known: Sequence[str] = (),
        force: bool = False,
    ) -> Classification:
        """Classify player text. Never raises, never writes state.

        ``force`` asks the model even when the rules are confident; that is for
        an author clicking "analyse" in the editor, not for a game turn.
        """
        local = classify_locally(text)
        if not force and not needs_agent(local.statements):
            return local
        if self.agents is None:
            local.warnings.append("system agent unavailable; used rules only")
            local.degraded = True
            return local
        config = self.agents.config("system")
        if not config.enabled:
            local.warnings.append("system agent disabled; used rules only")
            local.degraded = True
            return local
        messages = _agent_messages(text, known, self.language)
        reply: AgentReply = await self.agents.ask("system", messages, task="classify")
        local.consulted_agent = True
        if not reply.ok:
            local.degraded = True
            local.error = reply.error
            local.warnings.append("system agent failed; used rules only")
            return local
        payload = extract_json_object(reply.text)
        statements = parse_agent_payload(payload)
        if not statements:
            local.degraded = True
            local.warnings.append("system agent returned nothing usable; used rules only")
            return local
        merged = _merge(local.statements, statements, self.min_confidence)
        local.statements = merged
        local.conflicts = [
            (f"{s.text} conflicts with {s.conflicts_with}" if s.conflicts_with else s.text)
            for s in merged
            if s.conflict
        ]
        return local

    async def review_ops(
        self,
        text: str,
        *,
        known: Sequence[str] = (),
    ) -> List[Dict[str, Any]]:
        """Ask for authoring operations; return only structurally valid ones."""
        if self.agents is None:
            return []
        messages = _agent_messages(text, known, self.language)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Return authoring operations as JSON:"
                    ' {"ops": [{"kind": "' + "|".join(OP_KINDS) + '", "data": {...}}]}.'
                    " Only include operations the text clearly supports."
                ),
            }
        )
        reply = await self.agents.ask("system", messages, task="plan_ops")
        if not reply.ok:
            logger.warning("system agent could not plan operations: %s", reply.error)
            return []
        payload = extract_json_object(reply.text) or {}
        items = payload.get("ops")
        if not isinstance(items, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            data = item.get("data")
            if kind not in OP_KINDS or not isinstance(data, dict):
                continue
            out.append({"kind": kind, "data": data})
        return out


def _merge(
    local: Sequence[Statement],
    agent: Sequence[Statement],
    min_confidence: float,
) -> List[Statement]:
    """Model statements win per sentence; rules fill sentences it skipped."""
    by_text = {s.text: s for s in agent if s.confidence >= min_confidence}
    merged: List[Statement] = []
    seen = set()
    for statement in local:
        chosen = by_text.get(statement.text) or statement
        merged.append(chosen)
        seen.add(statement.text)
    for text, statement in by_text.items():
        if text not in seen:
            merged.append(statement)
    return merged


def classify_sync(text: str) -> Classification:
    """Rules-only classification, for callers that cannot await."""
    return classify_locally(text)


def is_canon_candidate(statement: Statement, min_confidence: float = 0.6) -> bool:
    """Whether a statement may be written to canon without human approval."""
    if statement.kind not in CANON_KINDS:
        return False
    if statement.conflict:
        return False
    return statement.confidence >= min_confidence
