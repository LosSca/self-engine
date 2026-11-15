"""Entity resolution for memory retrieval (P2).

A player asks "what happened to that knight who rescued me?". The world knows
her by three names - the Chinese, the katakana and the Latin spelling - and
the store keeps them as one entity with the others as aliases. Keyword search
alone cannot make that jump, so every retrieval path runs its text through
this resolver first and queries with the canonical name.

The resolver is deliberately dictionary-based rather than model-based:

* it is deterministic, so a retrieval test can assert an exact answer;
* it works offline, so the default install still resolves aliases;
* it fails open - an unknown name resolves to itself, and nothing is ever
  dropped because it was not in the dictionary.

Script-insensitive matching is what makes multilingual recall work: a CJK name
inside Latin prose is found by scanning for the alias as a substring, and a
Latin name inside CJK prose is found the same way, because both sides are
compared after NFKC + casefold normalization.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from app.models.memory import normalize_alias

logger = logging.getLogger(__name__)

#: Entity kinds the memory layer distinguishes. Anything else is stored as
#: "entity" and still resolves; the kind only drives presentation.
ENTITY_KINDS = ("person", "place", "organization", "item", "concept")

#: A Latin alias must be a whole word to match, otherwise "Rin" fires inside
#: "grin" and "printer". A CJK alias is matched as a substring because CJK
#: prose has no spaces.
_ENTITY_TYPE_HINTS = {
    "person": ("person", "npc", "character"),
    "place": ("place", "location", "region"),
    "organization": ("organization", "faction", "group"),
    "item": ("item", "artifact", "weapon"),
}


def looks_latin(text: str) -> bool:
    """True when the text has Latin letters and no CJK. Picks a matcher."""
    has_latin = False
    for char in str(text or ""):
        if "\u3040" <= char <= "\u9fff" or "\uac00" <= char <= "\ud7af":
            return False
        if char.isalpha():
            has_latin = True
    return has_latin


@dataclass(frozen=True)
class EntityMention:
    """One alias found in a text, pointing at its canonical entity.

    `start`/`end` are offsets in the *normalized* text the resolver scanned, so
    a caller can highlight the mention in the original.
    """

    entity_id: str
    canonical_name: str
    alias: str
    kind: str = "entity"
    start: int = 0
    end: int = 0


@dataclass
class EntityIndex:
    """Canonical name -> aliases, and the reverse lookup aliases need."""

    #: alias (normalized) -> canonical name
    aliases: Dict[str, str] = field(default_factory=dict)
    #: canonical name -> entity id and kind
    entities: Dict[str, Dict[str, str]] = field(default_factory=dict)
    #: canonical name -> every alias, canonical first
    alias_lists: Dict[str, List[str]] = field(default_factory=dict)

    def add(
        self,
        canonical_name: str,
        aliases: Iterable[str] = (),
        *,
        entity_id: str = "",
        kind: str = "entity",
    ) -> str:
        """Register one entity; returns the canonical name actually used.

        Re-registering a name that is already an alias of another entity keeps
        the first registration: two characters that happen to share a name is
        rarer than a model writing the same character two ways, and keeping the
        first owner is the behaviour that merges the fewest distinct people.
        """
        name = str(canonical_name or "").strip()
        if not name:
            return ""
        key = normalize_alias(name)
        existing = self.aliases.get(key)
        if existing and existing != name:
            name = existing
            key = normalize_alias(name)
        self.entities.setdefault(
            name, {"entity_id": entity_id or key, "kind": kind or "entity", "canonical": name}
        )
        names = self.alias_lists.setdefault(name, [name])
        for alias in (canonical_name, *aliases):
            alias = str(alias or "").strip()
            if not alias:
                continue
            alias_key = normalize_alias(alias)
            if not alias_key:
                continue
            owner = self.aliases.get(alias_key)
            if owner and owner != name:
                # Another entity already owns this alias. Keep the owner: a
                # silent merge would make one character's memories answer for
                # another's.
                logger.debug(
                    "entity alias %r belongs to %r; not re-registering for %r",
                    alias, owner, name,
                )
                continue
            self.aliases[alias_key] = name
            if alias not in names:
                names.append(alias)
        return name

    def canonical(self, name: str) -> str:
        """The canonical name for a known alias, or "" for an unknown name.

        Returning "" rather than the input matters to every caller that asks
        "is this name one of ours": the fact channel compares canonical names
        for equality, and echoing the input would make every stray word in a
        query look like a resolved entity.
        """
        key = normalize_alias(name)
        return self.aliases.get(key, "")

    def aliases_of(self, name: str) -> List[str]:
        canonical = self.canonical(name)
        return list(self.alias_lists.get(canonical, [canonical] if canonical else []))

    def kind_of(self, name: str) -> str:
        canonical = self.canonical(name)
        return self.entities.get(canonical, {}).get("kind", "entity")

    def lookup(self, entity_id: str) -> Optional[Dict[str, str]]:
        for record in self.entities.values():
            if record.get("entity_id") == entity_id:
                return dict(record)
        return None

    def names(self) -> List[str]:
        return list(self.entities)

    def __len__(self) -> int:
        return len(self.entities)


def build_entity_index(entities: Sequence) -> EntityIndex:
    """Build an index from MemoryEntity rows (or anything with the same fields)."""
    index = EntityIndex()
    for entity in entities or []:
        name = getattr(entity, "canonical_name", "") or ""
        if not name:
            continue
        index.add(
            name,
            getattr(entity, "aliases", []) or [],
            entity_id=getattr(entity, "id", "") or "",
            kind=getattr(entity, "entity_type", "") or "entity",
        )
    return index


def _alias_pattern(alias: str) -> "re.Pattern":
    normalized = unicodedata.normalize("NFKC", alias).casefold()
    if looks_latin(normalized):
        return re.compile(r"(?<![^\W_])" + re.escape(normalized) + r"(?![^\W_])")
    return re.compile(re.escape(normalized))


def find_mentions(text: str, index: EntityIndex, *, limit: int = 24) -> List[EntityMention]:
    """Every alias occurrence in `text`, longest alias first, no overlaps.

    The longest-match-wins rule matters: "White Pearl" and "Pearl" can both be
    aliases of the same ship, and the two-word form is the one the text meant.
    """
    if not text or not index.aliases:
        return []
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    found: List[EntityMention] = []
    taken: List[tuple] = []
    alias_items = sorted(index.aliases.items(), key=lambda item: -len(item[0]))
    for alias_key, canonical in alias_items:
        if not alias_key:
            continue
        pattern = _alias_pattern(alias_key)
        record = index.entities.get(canonical, {})
        for match in pattern.finditer(normalized):
            span = (match.start(), match.end())
            if any(not (span[1] <= s or span[0] >= e) for s, e in taken):
                continue
            taken.append(span)
            found.append(
                EntityMention(
                    entity_id=record.get("entity_id", alias_key),
                    canonical_name=canonical,
                    alias=match.group(0),
                    kind=record.get("kind", "entity"),
                    start=span[0],
                    end=span[1],
                )
            )
            if len(found) >= limit:
                return found
    found.sort(key=lambda mention: mention.start)
    return found


def mentioned_names(text: str, index: EntityIndex) -> List[str]:
    """Canonical names mentioned in `text`, in reading order, deduplicated."""
    names: List[str] = []
    for mention in find_mentions(text, index):
        if mention.canonical_name not in names:
            names.append(mention.canonical_name)
    return names


def expand_query_entities(names: Iterable[str], index: EntityIndex) -> List[str]:
    """Canonical names for a bag of names, plus every alias of each.

    Retrieval uses this so a query written with one name still matches a
    document written with the other.
    """
    out: List[str] = []
    seen = set()
    for name in names:
        for candidate in index.aliases_of(name) if name else []:
            candidate = str(candidate or "").strip()
            key = candidate.casefold()
            if candidate and key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


def hint_kind(entity_type: str) -> str:
    """Map a free-form type label onto one of ENTITY_KINDS (best effort)."""
    lowered = str(entity_type or "").strip().casefold()
    if lowered in ENTITY_KINDS:
        return lowered
    for kind, hints in _ENTITY_TYPE_HINTS.items():
        if any(hint in lowered for hint in hints):
            return kind
    return "entity"


def describe_entity(canonical_name: str, index: EntityIndex) -> str:
    """One-line description for the context pack ("Eileen (person; also 艾琳)")."""
    canonical = index.canonical(canonical_name)
    record = index.entities.get(canonical)
    if record is None:
        return canonical
    aliases = [a for a in index.aliases_of(canonical) if a != canonical]
    kind = record.get("kind", "entity") or "entity"
    if aliases:
        return f"{canonical} ({kind}: also {', '.join(aliases[:3])})"
    return f"{canonical} ({kind})"
