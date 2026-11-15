"""Theme-driven consolidation of memory crystals (P2).

The old cascade merged the oldest four unconsumed crystals of a tier, in
time order, and repeated until the tier emptied. That is a bounded-memory
filter, not a memory: it grouped whatever happened to be adjacent, it split
one story arc across two merged summaries, and a single pivotal event could
be merged into a paragraph with three uneventful ones the moment it stopped
being among the newest.

This module decides *what belongs together* before anything is merged:

* crystals are grouped by the entities and places they share (a plot thread),
  then by the project they were tagged with, then by time;
* a group must reach CONSOLIDATION_COUNT to be merged, so nothing is
  paraphrased away early;
* a crystal flagged `always_include` (a one-off pivot the engine decided the
  narrator must keep) is never merged at all;
* every merged crystal keeps the ids of the crystals it replaced, so the
  summary can always be traced back to the record it paraphrases.

Grouping is deterministic: the same crystals always produce the same plan, so
a consolidation run can be replayed and asserted in a test.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.models.memory import normalize_alias
from app.services.retrieval.tokenizer import tokenize_text

logger = logging.getLogger(__name__)

#: How consolidation decides what belongs together.
CONSOLIDATION_TIME = "time"      # the historical behaviour: oldest N, in order
CONSOLIDATION_THEME = "theme"    # group by entity / place / thread first

#: Names that appear in almost every crystal and therefore identify nothing.
_GENERIC_ENTITIES = frozenset(
    name.casefold()
    for name in (
        "player", "you", "they", "narrator", "world", "the party",
        "玩家", "你们", "他们", "队伍", "世界",
    )
)


@dataclass(frozen=True)
class ConsolidationTheme:
    """One group of crystals that belong to the same thread."""

    key: str
    label: str
    doc_ids: Tuple[str, ...]
    kind: str = "thread"

    @property
    def size(self) -> int:
        return len(self.doc_ids)


@dataclass
class ConsolidationPlan:
    """What one consolidation pass would merge, and what it leaves alone."""

    themes: List[ConsolidationTheme] = field(default_factory=list)
    skipped_pinned: List[str] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)
    mode: str = CONSOLIDATION_THEME

    @property
    def mergeable(self) -> List[ConsolidationTheme]:
        return [theme for theme in self.themes if theme.doc_ids]

    def merged_ids(self) -> List[str]:
        return [doc_id for theme in self.themes for doc_id in theme.doc_ids]

    def as_dict(self) -> Dict[str, object]:
        return {
            "mode": self.mode,
            "themes": [
                {"key": t.key, "label": t.label, "kind": t.kind, "doc_ids": list(t.doc_ids)}
                for t in self.themes
            ],
            "skipped_pinned": list(self.skipped_pinned),
            "deferred": list(self.deferred),
        }


def consolidation_mode() -> str:
    """MEMORY_CONSOLIDATION=time restores the old time-ordered cascade."""
    import os

    raw = os.environ.get("MEMORY_CONSOLIDATION", CONSOLIDATION_THEME).strip().lower()
    return CONSOLIDATION_TIME if raw in ("time", "legacy", "old", "0", "false", "off") else CONSOLIDATION_THEME


def _entity_keys(crystal) -> List[str]:
    """Identity keys for a crystal: its entities and its location, normalised."""
    keys: List[str] = []
    for name in getattr(crystal, "related_entities", None) or ():
        key = normalize_alias(name)
        if key and key not in _GENERIC_ENTITIES and key not in keys:
            keys.append(key)
    return keys


def _overlap(left: Sequence[str], right: Sequence[str]) -> int:
    return len(set(left) & set(right))


def _time_of(crystal) -> str:
    return str(getattr(crystal, "source_start_created_at", "") or "")


#: An entity-free scene joins a thread only when it happened within this many
#: seconds of one of its members (roughly one narrative day of that tier). It
#: is what separates "the walk back from the gate" from "three chapters later".
UNTAGGED_MAX_GAP_SECONDS = 6 * 60 * 60


def _attach_untagged(clusters: List[Tuple[List[str], List]], untagged: Sequence) -> None:
    """File each entity-free crystal under the thread it sits between in time.

    An ambient scene ("you walk through the market") belongs to whatever arc
    was running when it happened. Attaching it is what stops it from either
    forming its own meaningless thread or being dropped from consolidation
    entirely; when there is no thread to attach to it stays untagged and the
    caller defers it.
    """
    if not untagged or not clusters:
        return
    # The scene's own place in the story, before anything was attached: a scene
    # that happens after every member of a thread is the start of the next
    # stretch, not part of this one.
    thread_spans = [
        (min((_time_of(m) for m in members), default=""), max((_time_of(m) for m in members), default=""), members)
        for _keys, members in clusters
    ]
    for crystal in sorted(untagged, key=_time_of):
        when = _time_of(crystal)
        attached_to = None
        best_gap = None
        # An undated scene cannot be proven adjacent, so it stays unmerged
        # rather than joining whatever thread was read last.
        if when:
            for _start, end, members in thread_spans:
                if not end:
                    continue
                gap = _distance(when, end)
                if gap is None or gap > UNTAGGED_MAX_GAP_SECONDS:
                    continue
                if best_gap is None or gap < best_gap:
                    attached_to, best_gap = members, gap
        # Only an adjacent scene joins the thread. Without this cap a single
        # cluster absorbs every quiet scene in the campaign and the merge slides
        # back to the old "oldest four, whatever they were".
        if attached_to is None:
            continue
        attached_to.append(crystal)
        end_times = [t for t in (_time_of(m) for m in attached_to) if t]
        if end_times:
            for index, span in enumerate(thread_spans):
                if span[2] is attached_to:
                    thread_spans[index] = (span[0], max(end_times), attached_to)


def _distance(left: str, right: str):
    """Absolute difference between two ISO-ish timestamps, or None.

    Strings are compared lexicographically when they cannot be parsed, which is
    still a usable ordering for the timestamp format this project writes.
    """
    if not left or not right:
        return None
    try:
        from datetime import datetime

        return abs((datetime.fromisoformat(left) - datetime.fromisoformat(right)).total_seconds())
    except (TypeError, ValueError):
        return abs(len(left) - len(right)) if left == right else None


def group_crystals(
    crystals: Sequence,
    *,
    min_size: int = 4,
    label_of=None,
) -> Tuple[List[ConsolidationTheme], List[str], List[str]]:
    """Group crystals into threads. Returns (themes, skipped_pinned, deferred).

    `label_of` maps a key to a display label; the default is the key itself.
    A crystal with no entities joins the thread it shares a tier and a
    neighbouring time span with, which is what keeps untagged prose (an
    ambient scene) from forming a thread of its own on every turn.
    """
    label = label_of or (lambda key: key)
    pinned: List[str] = []
    remaining: List = []
    for crystal in crystals or ():
        doc_id = getattr(crystal, "memory_id", "") or str(id(crystal))
        if getattr(crystal, "always_include", False):
            pinned.append(doc_id)
            continue
        remaining.append(crystal)

    # Greedy clustering by shared entity, oldest crystal first so a thread is
    # built in the order the player lived it.
    #
    # A crystal that names no entity is a scene, not a thread: it is grouped by
    # adjacency instead (see _attach_untagged below), because letting two
    # untagged scenes cluster together on an empty key would merge unrelated
    # ambient prose and hide whatever actually happened in it.
    clusters: List[Tuple[List[str], List]] = []
    untagged: List = []
    for crystal in remaining:
        keys = _entity_keys(crystal)
        if not keys:
            untagged.append(crystal)
            continue
        placed = False
        for cluster_keys, members in clusters:
            if _overlap(keys, cluster_keys) >= 1:
                cluster_keys.extend(k for k in keys if k not in cluster_keys)
                members.append(crystal)
                placed = True
                break
        if not placed:
            clusters.append((list(keys), [crystal]))
    _attach_untagged(clusters, untagged)

    # Everything attached to a thread is a thread member, even if nothing in
    # that group carries an entity of its own (four untagged scenes in one run
    # of play are still one stretch of story). Only a *separate* thread needs
    # an entity to be worth merging.
    if untagged and not clusters:
        clusters.append(([], list(remaining)))

    # A pass merges one thread, and a thread needs at least two members to be a
    # merge at all. The old count of four is still honoured as a *target*: the
    # engine keeps folding into the same theme key until the thread is worth
    # one summary, and the plan reports how far along it is. Waiting for four
    # scenes of the same thread before summarising anything is what used to
    # leave a campaign's early chapters unsummarised for dozens of turns.
    quota = 2

    theme_by_doc: Dict[str, str] = {}
    themes: List[ConsolidationTheme] = []
    deferred: List[str] = []
    for keys, members in clusters:
        members.sort(key=lambda c: (getattr(c, "source_start_created_at", "") or "", getattr(c, "memory_id", "")))
        doc_ids = tuple(getattr(c, "memory_id", "") or str(id(c)) for c in members)
        if len(members) < quota:
            # Too thin to be a thread of its own; those crystals stay free for
            # a later pass instead of being merged into somebody else's story.
            deferred.extend(doc_ids)
            continue
        for doc_id in doc_ids:
            theme_by_doc[doc_id] = keys[0] if keys else doc_id
        key = keys[0] if keys else (doc_ids[0] if doc_ids else "")
        themes.append(
            ConsolidationTheme(
                key=key,
                label=label(key),
                doc_ids=doc_ids,
                kind="thread" if keys else "span",
            )
        )

    for crystal in remaining:
        doc_id = getattr(crystal, "memory_id", "") or str(id(crystal))
        if doc_id in theme_by_doc:
            continue
        if doc_id not in deferred:
            deferred.append(doc_id)

    themes.sort(key=lambda theme: -theme.size)
    return themes, pinned, deferred


def plan_consolidation(
    crystals: Sequence,
    *,
    count: int = 4,
    mode: Optional[str] = None,
    label_of=None,
) -> ConsolidationPlan:
    """Decide which crystals the next consolidation pass merges.

    In theme mode exactly one thread is merged per pass - the largest, with
    ties broken by the newest member - because merging several threads at once
    is what made a summary lose the distinction between them.
    In time mode the plan is the historical one: the oldest `count` unconsumed
    crystals, in order, provided there are at least `count` of them.
    """
    chosen_mode = mode or consolidation_mode()
    plan = ConsolidationPlan(mode=chosen_mode)
    candidates = [c for c in crystals or () if not getattr(c, "consumed", False)]
    if len(candidates) < max(1, int(count)):
        plan.deferred = [getattr(c, "memory_id", "") or str(id(c)) for c in candidates]
        return plan

    if chosen_mode == CONSOLIDATION_TIME:
        ordered = sorted(
            candidates,
            key=lambda c: (getattr(c, "source_start_created_at", "") or "", getattr(c, "memory_id", "")),
        )
        merged = ordered[: max(1, int(count))]
        plan.themes = [
            ConsolidationTheme(
                key="time",
                label="time",
                doc_ids=tuple(getattr(c, "memory_id", "") or str(id(c)) for c in merged),
                kind="time",
            )
        ]
        return plan

    themes, pinned, deferred = group_crystals(candidates, min_size=count, label_of=label_of)
    plan.skipped_pinned = pinned
    plan.deferred = deferred
    if themes:
        plan.themes = [themes[0]]
    return plan


def select_merge_batch(
    crystals: Sequence,
    *,
    count: int = 4,
    mode: Optional[str] = None,
    label_of=None,
) -> List:
    """The crystal objects one consolidation pass should merge (possibly none).

    Callers that only need the batch (the engine cascade) use this; callers
    that want to explain or record the decision use plan_consolidation.
    """
    plan = plan_consolidation(crystals, count=count, mode=mode, label_of=label_of)
    if not plan.themes:
        return []
    wanted = set(plan.themes[0].doc_ids)
    by_id = {getattr(c, "memory_id", "") or str(id(c)): c for c in crystals or ()}
    batch = [by_id[doc_id] for doc_id in plan.themes[0].doc_ids if doc_id in by_id]
    # A batch is never larger than the count the caller agreed to, even when
    # the thread it came from is longer: the merge prompt is sized for count
    # crystals, and what is left of the thread is picked up by the next pass
    # with the same theme key. Truncating here (rather than in the planner)
    # keeps the plan itself an honest description of the thread.
    return batch[: max(1, int(count))]


def theme_keywords(crystals: Sequence, *, limit: int = 12) -> List[str]:
    """Words shared by a thread, for the merge prompt and the context pack.

    Shared keywords are what make a merged summary read like one story: they
    are taken from the crystals the merge is about to replace, weighted by how
    many of them mention the word.
    """
    counts: Dict[str, int] = {}
    for crystal in crystals or ():
        text = (getattr(crystal, "ai_content", "") or "") + " " + (getattr(crystal, "content", "") or "")
        for token in set(tokenize_text(text)):
            counts[token] = counts.get(token, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, hits in ordered if hits > 1][:limit]


def describe_plan(plan: ConsolidationPlan) -> str:
    """One-line summary of a plan, for logs and the debug panel."""
    if not plan.themes:
        return f"consolidation ({plan.mode}): nothing mergeable ({len(plan.deferred)} deferred)"
    theme = plan.themes[0]
    return (
        f"consolidation ({plan.mode}): thread {theme.label!r} "
        f"merges {theme.size} crystals ({len(plan.deferred)} deferred, "
        f"{len(plan.skipped_pinned)} pinned)"
    )
