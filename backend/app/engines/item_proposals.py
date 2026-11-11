"""Item proposals: the migration path away from prose-authored inventory.

Narration used to mutate the inventory directly through inline tags
(`[ITEM_ADD:name|category|source]`, `[ITEM_USE:name]`, `[ITEM_LOSE:name]`).
That made model output the authority for item state, which is exactly what the
engine is supposed to prevent: rules decide facts, prose describes them.

This module turns those tags into *proposals*. A proposal is validated data; it
is recorded for inspection and does not change state unless the legacy switch
`LEGACY_APPLY_ITEM_TAGS=1` is set during the migration window. The rule engine
never reads proposals - authorized item changes arrive as `give_item` /
`remove_item` effects from the deterministic pipeline instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Sequence, Tuple

ProposalAction = Literal["add", "use", "lose"]
ACTIONS: Tuple[str, ...] = ("add", "use", "lose")
MAX_NAME = 120
MAX_CATEGORY = 60
MAX_SOURCE = 200

_TAG_RE = re.compile(
    r"\[ITEM_(?:ADD:([^|\]]+)\|([^|\]]+)\|([^\]]+)|USE:([^\]]+)|LOSE:([^\]]+))\]"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class ItemProposal:
    """One validated item request emitted by the narrator."""

    action: ProposalAction
    name: str
    category: str = "misc"
    source: str = "narrator"
    turn: int = 0
    status: Literal["proposed", "applied", "rejected"] = "proposed"
    reason: str = ""
    key: str = ""

    def as_event_payload(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "name": self.name,
            "category": self.category,
            "source": self.source,
            "turn": self.turn,
            "status": self.status,
            "reason": self.reason,
        }


def _clean(value: Any, limit: int, default: str = "") -> str:
    if value is None:
        return default
    text = _CONTROL_RE.sub(" ", str(value)).strip()
    return text[:limit]


def make_proposal(
    action: Any, name: Any, category: Any = "misc", source: Any = "narrator", turn: int = 0
) -> ItemProposal:
    """Normalize raw tag fields into a proposal (never raises)."""
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in ACTIONS:
        normalized_action = "add"
    clean_name = _clean(name, MAX_NAME)
    return ItemProposal(
        action=normalized_action,  # type: ignore[arg-type]
        name=clean_name,
        category=_clean(category, MAX_CATEGORY, "misc") or "misc",
        source=_clean(source, MAX_SOURCE, "narrator") or "narrator",
        turn=int(turn or 0),
        key=f"{turn}:{normalized_action}:{clean_name.lower()}",
    )


def validate_proposal(proposal: ItemProposal) -> str:
    """Return a readable rejection reason, or "" when the proposal is well formed."""
    if proposal.action not in ACTIONS:
        return f"未知的物品操作：{proposal.action!r}"
    if not proposal.name:
        return "物品名为空"
    if len(proposal.name) > MAX_NAME:
        return f"物品名过长（超过 {MAX_NAME} 个字符）"
    if proposal.action != "add" and not proposal.name.strip():
        return "缺少物品名"
    return ""


def proposals_from_text(text: str, turn: int = 0) -> Tuple[str, List[ItemProposal]]:
    """Split narrator text into clean prose plus validated item proposals.

    Tags are stripped from the prose in every case so control syntax never
    reaches the player, whether or not the proposal is later authorized.
    """
    proposals: List[ItemProposal] = []
    for match in _TAG_RE.finditer(text or ""):
        if match.group(1) is not None:
            proposal = make_proposal(
                "add", match.group(1), match.group(2), match.group(3), turn
            )
        elif match.group(4) is not None:
            proposal = make_proposal("use", match.group(4), turn=turn)
        else:
            proposal = make_proposal("lose", match.group(5), turn=turn)
        proposals.append(proposal)
    clean = _TAG_RE.sub("", text or "")
    return clean, proposals


@dataclass
class ProposalBatch:
    """Deduplicated proposals for one narrator turn."""

    proposals: List[ItemProposal] = field(default_factory=list)
    duplicates: int = 0

    def add(self, proposal: ItemProposal) -> bool:
        if any(existing.key == proposal.key for existing in self.proposals):
            self.duplicates += 1
            return False
        self.proposals.append(proposal)
        return True

    def as_payloads(self) -> List[Dict[str, Any]]:
        return [proposal.as_event_payload() for proposal in self.proposals]


def dedupe(proposals: Iterable[ItemProposal]) -> ProposalBatch:
    batch = ProposalBatch()
    for proposal in proposals:
        batch.add(proposal)
    return batch


def describe(proposal: Sequence[ItemProposal]) -> str:
    return ", ".join(f"{p.action}:{p.name}" for p in proposal)
