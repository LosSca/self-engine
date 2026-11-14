"""Knowledge-graph question analysis and multi-hop relationship recall.

The graph channel used to be a single `search(query, group_ids, limit=10)`
call with whatever text happened to be around. Three things were missing:

1. **Entity focus.** A graph is indexed by entity, so the query must name
   entities. :func:`extract_entities` pulls candidate names out of a scene -
   proper nouns, quoted names, CJK runs that look like names, and the known
   NPC roster - and rejects the words that only look capitalised.
2. **Alias resolution.** "老巴克" / "巴克" / "Bark" are the same node. The
   resolver folds an alias onto the canonical name that the graph or the mind
   store already knows.
3. **Temporal validity.** A fact with `invalid_at <= now` is history, not
   current truth; it is dropped from recall (or kept only when the caller is
   explicitly asking what used to be true).

Everything here is pure: no I/O, no graph client, so it is testable without a
Neo4j instance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set

_CJK_NAME_RE = re.compile("[\u4e00-\u9fff]{2,4}")
_LATIN_NAME_RE = re.compile(r"\b[A-Z][a-zA-Z'’-]{1,30}(?:\s+[A-Z][a-zA-Z'’-]{1,30})*\b")
_QUOTED_RE = re.compile(r"[「『“\"']([^「」『』“”\"']{2,30})[」』”\"']")

#: Words that are capitalised only because they start a sentence, or that name
#: a game concept rather than an entity. Without this the graph channel gets
#: flooded with "The", "You" and "Player".
_NON_ENTITY_WORDS = frozenset(
    """
    the a an and or but if then than this that these those there here
    i you he she it we they me him her us them my your his her its our their
    yes no not now then when where what who how why player narrator god
    上一轮 这一轮 当前 玩家 系统 旁白 世界 场景 任务 攻击 防御 生命 法力
    """.split()
)


@dataclass(frozen=True)
class EntityMention:
    """One candidate entity name and how confident the extractor is.

    `source` records the rule that produced the name ("roster", "quoted",
    "proper_noun", "cjk_name") so a debug panel can explain a recall that looks
    surprising, and so the resolver can prefer a roster hit over a guess.
    """

    name: str
    source: str
    confidence: float = 0.5


@dataclass
class GraphQuery:
    """What to ask the graph, after alias folding and validity filtering."""

    entities: List[str] = field(default_factory=list)
    text: str = ""
    include_invalid: bool = False
    max_hops: int = 2

    def is_empty(self) -> bool:
        return not self.entities and not self.text.strip()


def extract_entities(
    text: str,
    known_names: Sequence[str] = (),
    limit: int = 8,
) -> List[EntityMention]:
    """Candidate entity names in `text`, best first.

    Names already known (the NPC roster, the graph's node list) are matched by
    substring, because that is how they appear in prose: "和巴克说话" contains
    the roster name "巴克" but no capital letter and no quotes.
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    found: Dict[str, EntityMention] = {}

    def _add(name: str, source: str, confidence: float) -> None:
        cleaned = name.strip()
        if not cleaned or cleaned.casefold() in _NON_ENTITY_WORDS:
            return
        existing = found.get(cleaned)
        if existing is None or confidence > existing.confidence:
            found[cleaned] = EntityMention(cleaned, source, confidence)

    # 1. Roster match: strongest signal, and the only one that works for a
    #    name the prose refers to indirectly ("那个铁匠").
    #
    #    Prose also uses the *short* form of a roster name ("老巴克" is addressed
    #    as "巴克"), so a roster entry is matched by its full form and by its
    #    suffix of three or more characters. Two characters is not enough
    #    ("汉森" alone would fire on any unrelated text containing those
    #    characters), so the alias resolver keeps handling those explicitly.
    lowered = raw.casefold()
    for name in known_names:
        candidate = str(name or "").strip()
        if len(candidate) < 2:
            continue
        folded = candidate.casefold()
        if folded in lowered:
            _add(candidate, "roster", 1.0)
            continue
        for length in range(len(candidate) - 1, 1, -1):
            short = candidate[-length:]
            if short.casefold() in lowered:
                _add(candidate, "roster_short", 0.8)
                break

    # 2. Quoted names: an author quoting a name is unambiguous.
    for match in _QUOTED_RE.findall(raw):
        _add(match, "quoted", 0.9)

    # 3. Capitalised Latin words ("Old Bark") outside the stop list.
    for match in _LATIN_NAME_RE.findall(raw):
        if match.casefold() not in _NON_ENTITY_WORDS:
            _add(match, "proper_noun", 0.6)

    # 4. Short CJK runs. Chinese has no case, so the rule is length plus the
    #    stop list; a longer run is a phrase, not a name.
    for match in _CJK_NAME_RE.findall(raw):
        if match in _NON_ENTITY_WORDS:
            continue
        if any(match in known for known in known_names if known):
            _add(match, "roster", 1.0)
        else:
            _add(match, "cjk_name", 0.3)

    ordered = sorted(found.values(), key=lambda m: (-m.confidence, m.name))
    return ordered[: max(1, limit)]


def resolve_aliases(
    names: Iterable[str],
    canonical_names: Sequence[str],
    aliases: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Fold every name onto its canonical form, dropping duplicates.

    Resolution order: an explicit alias table wins; otherwise a name that *is*
    a canonical name is kept; otherwise a name contained in exactly one
    canonical name is folded onto it (「巴克」→「老巴克」); otherwise the name is
    kept as typed, because a graph may hold nodes the roster does not know.
    """
    table = {str(k).strip().casefold(): str(v).strip() for k, v in (aliases or {}).items()}
    canonical = [str(name).strip() for name in canonical_names if str(name or "").strip()]
    canonical_folded = {name.casefold(): name for name in canonical}
    out: List[str] = []
    seen: Set[str] = set()

    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        folded = name.casefold()
        resolved = table.get(folded, "")
        if not resolved:
            resolved = canonical_folded.get(folded, "")
        if not resolved:
            matches = [c for c in canonical if folded in c.casefold()]
            if len(matches) == 1:
                resolved = matches[0]
        resolved = resolved or name
        key = resolved.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def _parse_timestamp(value) -> Optional[datetime]:
    """ISO-8601 (with or without a zone) or a datetime; anything else is None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_fact_valid(fact: Dict[str, object], now: Optional[datetime] = None) -> bool:
    """A fact is current unless its `invalid_at` has already passed.

    Graphiti writes `invalid_at` when a newer episode contradicts an edge; the
    node still exists, but as history. Recall for the narrator's "what is true
    now" must not resurrect it.
    """
    invalid_at = _parse_timestamp(fact.get("invalid_at"))
    if invalid_at is None:
        return True
    reference = now or datetime.now(timezone.utc)
    return invalid_at > reference


def filter_valid(
    facts: Sequence[Dict[str, object]],
    include_invalid: bool = False,
    now: Optional[datetime] = None,
) -> List[Dict[str, object]]:
    """Drop expired facts unless the caller asked for history explicitly."""
    if include_invalid:
        return list(facts)
    return [fact for fact in facts if is_fact_valid(fact, now)]


def order_by_recency(facts: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
    """Newest `valid_at` first, facts without a timestamp last but stable.

    The graph returns edges in traversal order, which is meaningless to a
    reader. When two facts conflict, the newer one is the one to trust, so
    recency is the ordering the narrator should see.
    """
    def _key(item: tuple) -> tuple:
        position, fact = item
        stamp = _parse_timestamp(fact.get("valid_at"))
        if stamp is None:
            return (1, 0.0, position)
        return (0, -stamp.timestamp(), position)

    return [fact for _position, fact in sorted(enumerate(facts), key=_key)]


def build_graph_query(
    scene_text: str,
    known_names: Sequence[str] = (),
    focus: Sequence[str] = (),
    aliases: Optional[Dict[str, str]] = None,
    include_invalid: bool = False,
    max_hops: int = 2,
) -> GraphQuery:
    """The full graph query for one turn: entities, aliases folded, hops set."""
    mentions = extract_entities(
        " ".join([scene_text or "", *(str(item) for item in focus)]),
        known_names=known_names,
    )
    # Roster and quoted names are real signals; a bare CJK run is a guess that
    # happens to be four characters long. Guesses are only used when nothing
    # better was found, so the graph is not queried with "他走进酒".
    confident = [m.name for m in mentions if m.confidence >= 0.6]
    if not confident:
        confident = [m.name for m in mentions if m.source == "cjk_name"][:2]
    names = resolve_aliases(
        confident,
        canonical_names=list(known_names),
        aliases=aliases,
    )
    return GraphQuery(
        entities=names,
        text=" ".join(names) or (scene_text or "").strip(),
        include_invalid=include_invalid,
        max_hops=max(1, int(max_hops)),
    )


__all__ = [
    "EntityMention",
    "GraphQuery",
    "build_graph_query",
    "extract_entities",
    "filter_valid",
    "is_fact_valid",
    "order_by_recency",
    "resolve_aliases",
]
