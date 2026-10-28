"""Storyline engine: deterministic story progression for story-mode campaigns.

A storyline is authored data: nodes with prerequisites, optional conditions and
an optional required event. Progression is a pure function of the authoritative
state - the same state always yields the same entered nodes - so it can be
replayed, tested and debugged without any model call.

Free mode does not use this engine at all: it never enters story nodes, and the
knowledge context simply omits storyline-only entries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.engines.event_engine import condition_holds
from app.models.game import GameState, WorldDefinition
from app.models.knowledge import Storyline, StoryNode


@dataclass
class StoryStatus:
    """Result of evaluating a storyline against one state."""

    entered: List[str] = field(default_factory=list)
    newly_entered: List[str] = field(default_factory=list)
    active: Optional[str] = None
    pending: List[str] = field(default_factory=list)
    locked: List[str] = field(default_factory=list)
    reasons: Dict[str, str] = field(default_factory=dict)


def validate_storyline(
    storyline: Storyline,
    knowledge_ids: Optional[set] = None,
    event_ids: Optional[set] = None,
) -> List[str]:
    """Return readable errors; an empty list means the storyline is playable."""
    errors: List[str] = []
    nodes = storyline.nodes
    ids = [node.id for node in nodes]

    seen = set()
    for node_id in ids:
        if node_id in seen:
            errors.append(f"故事线节点 id 重复：'{node_id}'")
        seen.add(node_id)

    known = set(ids)
    for node in nodes:
        for prereq in node.prereq:
            if prereq == node.id:
                errors.append(f"故事线节点 '{node.id}' 不能以自身为前置")
            elif prereq not in known:
                errors.append(f"故事线节点 '{node.id}' 的前置节点 '{prereq}' 不存在")
        if node.kind == "branch" and not node.prereq:
            errors.append(f"分支节点 '{node.id}' 必须至少有一个前置节点")
        if node.kind == "main" and len(node.prereq) > 1:
            errors.append(f"主线节点 '{node.id}' 只能有一个前置节点")
        if knowledge_ids is not None:
            for knowledge_id in node.knowledge_ids:
                if knowledge_id not in knowledge_ids:
                    errors.append(
                        f"故事线节点 '{node.id}' 引用了不存在的知识条目 '{knowledge_id}'"
                    )
        if event_ids is not None and node.requires_event and node.requires_event not in event_ids:
            errors.append(
                f"故事线节点 '{node.id}' 依赖的事件 '{node.requires_event}' 不存在"
            )

    main_roots = [node for node in nodes if node.kind == "main" and not node.prereq]
    if nodes and not main_roots:
        errors.append("故事线至少需要一个没有前置的主线节点作为起点")

    # Cycle detection over the prerequisite graph (Kahn): if some nodes never
    # reach in-degree zero the remaining set contains at least one cycle.
    indegree = {node.id: 0 for node in nodes}
    children: Dict[str, List[str]] = {node.id: [] for node in nodes}
    for node in nodes:
        for prereq in node.prereq:
            if prereq in known:
                indegree[node.id] += 1
                children[prereq].append(node.id)
    queue = [node_id for node_id, degree in indegree.items() if degree == 0]
    processed = 0
    while queue:
        current = queue.pop()
        processed += 1
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if processed != len(nodes):
        remaining = [node_id for node_id, degree in indegree.items() if degree > 0]
        errors.append("故事线存在循环前置：" + " -> ".join(remaining))
    return errors


def storyline_status(
    storyline: Storyline,
    state: GameState,
    world: WorldDefinition,
    entered: Optional[List[str]] = None,
) -> StoryStatus:
    """Evaluate which nodes are entered / pending / locked for this state."""
    known = {node.id for node in storyline.nodes}
    entered_list: List[str] = []
    for node_id in entered if entered is not None else state.story_nodes:
        if node_id in known and node_id not in entered_list:
            entered_list.append(node_id)

    status = StoryStatus(entered=list(entered_list))
    entered_set = set(entered_list)
    changed = True
    passes = 0
    while changed and passes <= len(storyline.nodes):
        changed = False
        passes += 1
        for node in storyline.nodes:
            if node.id in entered_set:
                continue
            if not all(prereq in entered_set for prereq in node.prereq):
                continue
            reason = _blocking_reason(node, state, world)
            if reason:
                status.reasons.setdefault(node.id, reason)
                continue
            entered_set.add(node.id)
            entered_list.append(node.id)
            status.newly_entered.append(node.id)
            changed = True

    status.entered = entered_list
    for node in storyline.nodes:
        if node.id in entered_set:
            continue
        if all(prereq in entered_set for prereq in node.prereq):
            status.pending.append(node.id)
        else:
            status.locked.append(node.id)

    for node in reversed(entered_list):
        story_node = next((n for n in storyline.nodes if n.id == node), None)
        if story_node is not None and story_node.kind == "main":
            status.active = story_node.id
            break
    return status


def _blocking_reason(
    node: StoryNode, state: GameState, world: WorldDefinition
) -> str:
    if node.requires_event and node.requires_event not in state.fired_events:
        return f"需要先触发事件 '{node.requires_event}'"
    for condition in node.conditions:
        if not condition_holds(state, world, condition):
            target = condition.target or "-"
            return f"条件未满足：{condition.kind} {condition.op} {target}"
    return ""


def advance_storyline(
    storyline: Storyline, state: GameState, world: WorldDefinition
) -> StoryStatus:
    """Mutate state.story_nodes with newly entered nodes and return the status."""
    status = storyline_status(storyline, state, world, entered=state.story_nodes)
    if status.newly_entered:
        state.story_nodes = list(status.entered)
    return status
