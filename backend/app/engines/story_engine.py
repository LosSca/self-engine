"""Storyline engine: deterministic story progression for story-mode campaigns.

A storyline is authored data: nodes with prerequisites, optional conditions and
an optional required event. Progression is a pure function of the authoritative
state - the same state always yields the same entered nodes - so it can be
replayed, tested and debugged without any model call.

Three node kinds share the same engine:

* main   - the trunk, entered in prerequisite order;
* branch - hangs off an earlier node, so it belongs to one point of the story;
* side   - independent of the trunk: no prerequisites, entered when its own
  trigger holds. A side node therefore needs a condition or a required event,
  otherwise it would be entered the moment the campaign starts.

Event packs are optional bundles of world events: the engine reports which ones
are armed and which ones have fired, and a node may depend on a pack firing.

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
    #: Packs whose requirements currently hold (and which are not spent).
    armed_packs: List[str] = field(default_factory=list)
    #: Packs with at least one event recorded in state.fired_events.
    fired_packs: List[str] = field(default_factory=list)


def validate_storyline(
    storyline: Storyline,
    knowledge_ids: Optional[set] = None,
    event_ids: Optional[set] = None,
    entity_ids: Optional[set] = None,
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
    line_map = storyline.line_map()
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
        # An independent line must be *triggered*, not merely present: without a
        # trigger the node would be entered on the very first turn.
        if node.kind == "side" and not node.prereq and not node.conditions and not node.requires_event and not node.requires_event_pack:
            errors.append(
                f"独立故事线节点 '{node.id}' 没有前置节点，必须至少有一个条件或依赖事件，"
                "否则开局就会立即进入"
            )
        if node.line:
            line = line_map.get(node.line)
            if line is None:
                errors.append(f"故事线节点 '{node.id}' 归属的剧情线 '{node.line}' 不存在")
            elif line.kind != node.kind:
                errors.append(
                    f"故事线节点 '{node.id}' 的类型 '{node.kind}' 与剧情线 '{line.id}' 的类型 "
                    f"'{line.kind}' 不一致"
                )
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

    seen_lines = set()
    for line in storyline.lines:
        if line.id in seen_lines:
            errors.append(f"剧情线 id 重复：'{line.id}'")
        seen_lines.add(line.id)

    pack_ids = [pack.id for pack in storyline.packs]
    seen_packs = set()
    for pack in storyline.packs:
        if pack.id in seen_packs:
            errors.append(f"事件包 id 重复：'{pack.id}'")
        seen_packs.add(pack.id)
        if event_ids is not None:
            for event_id in pack.event_ids:
                if event_id not in event_ids:
                    errors.append(f"事件包 '{pack.id}' 引用了不存在的事件 '{event_id}'")
        if knowledge_ids is not None:
            for knowledge_id in pack.knowledge_ids:
                if knowledge_id not in knowledge_ids:
                    errors.append(
                        f"事件包 '{pack.id}' 引用了不存在的知识条目 '{knowledge_id}'"
                    )
        if not pack.event_ids:
            errors.append(f"事件包 '{pack.id}' 没有包含任何事件")
    for node in nodes:
        if node.requires_event_pack and node.requires_event_pack not in seen_packs:
            errors.append(
                f"故事线节点 '{node.id}' 依赖的事件包 '{node.requires_event_pack}' 不存在"
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


def pack_status(
    storyline: Storyline,
    state: GameState,
    world: WorldDefinition,
) -> tuple[List[str], List[str]]:
    """Return (armed, fired) pack ids for this state.

    A pack is fired when one of its events is in the authoritative fired set.
    It is armed while its own conditions hold; a once-only pack that already
    fired is spent and no longer armed.
    """
    fired: List[str] = []
    armed: List[str] = []
    for pack in storyline.packs:
        if not pack.enabled:
            continue
        ran = [event_id for event_id in pack.event_ids if event_id in state.fired_events]
        if ran:
            fired.append(pack.id)
        if ran and pack.once:
            continue
        if all(condition_holds(state, world, condition) for condition in pack.requires):
            armed.append(pack.id)
    return armed, fired


def _pack_fired(pack_id: str, storyline: Storyline, state: GameState) -> bool:
    pack = storyline.pack_map().get(pack_id)
    if pack is None:
        return False
    return any(event_id in state.fired_events for event_id in pack.event_ids)


def locked_node_ids(storyline: Storyline) -> set:
    """Ids of nodes an editor may not change: locked themselves or in a locked line."""
    locked_lines = {line.id for line in storyline.lines if line.locked}
    return {
        node.id
        for node in storyline.nodes
        if node.locked or (node.line and node.line in locked_lines)
    }


def protected_edits(previous: Storyline, updated: Storyline) -> List[str]:
    """Readable errors describing edits to settled (locked) content.

    Locking is an editorial guard, not a gameplay rule: it never changes what a
    campaign does, it only stops later edits from silently rewriting a story that
    is already running. Everything here compares two documents, so it stays a
    pure function and is cheap to test.
    """
    errors: List[str] = []
    if previous is None:
        return errors

    locked_lines = {line.id for line in previous.lines if line.locked}
    locked_nodes = locked_node_ids(previous)
    new_lines = {line.id: line for line in updated.lines}
    new_nodes = {node.id: node for node in updated.nodes}

    for line in previous.lines:
        if not line.locked:
            continue
        if line.id not in new_lines:
            errors.append(f"剧情线 '{line.id}' 已锁定，不能删除")
        elif new_lines[line.id] != line:
            errors.append(f"剧情线 '{line.id}' 已锁定，不能修改")

    for node in previous.nodes:
        if node.id not in locked_nodes:
            continue
        if node.id not in new_nodes:
            errors.append(f"故事线节点 '{node.id}' 已锁定，不能删除")
        elif new_nodes[node.id] != node:
            errors.append(f"故事线节点 '{node.id}' 已锁定，不能修改")

    # Existing nodes may not be smuggled into a locked line after the fact.
    for node in previous.nodes:
        if node.id in locked_nodes or node.id not in new_nodes:
            continue
        target = new_nodes[node.id]
        if target.line and target.line in locked_lines and target.line != node.line:
            errors.append(
                f"故事线节点 '{node.id}' 不能加入已锁定的剧情线 '{target.line}'"
            )
    return errors


def storyline_status(
    storyline: Storyline,
    state: GameState,
    world: WorldDefinition,
    entered: Optional[List[str]] = None,
) -> StoryStatus:
    """Evaluate which nodes are entered / pending / locked for this state."""
    known = {node.id for node in storyline.nodes}
    #: Switching a line off is editorial: its nodes keep their prerequisites but
    #: are never entered while the line is disabled.
    disabled_lines = {line.id for line in storyline.lines if not line.enabled}
    disabled_nodes = {
        node.id
        for node in storyline.nodes
        if node.line and node.line in disabled_lines
    }
    entered_list: List[str] = []
    for node_id in entered if entered is not None else state.story_nodes:
        if node_id in known and node_id not in entered_list:
            entered_list.append(node_id)

    status = StoryStatus(entered=list(entered_list))
    status.armed_packs, status.fired_packs = pack_status(storyline, state, world)
    entered_set = set(entered_list)
    changed = True
    passes = 0
    while changed and passes <= len(storyline.nodes):
        changed = False
        passes += 1
        for node in storyline.nodes:
            if node.id in entered_set:
                continue
            if node.id in disabled_nodes:
                status.reasons.setdefault(node.id, f"剧情线 '{node.line}' 已停用")
                continue
            if not all(prereq in entered_set for prereq in node.prereq):
                continue
            reason = _blocking_reason(node, state, world, storyline)
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
    node: StoryNode,
    state: GameState,
    world: WorldDefinition,
    storyline: Optional[Storyline] = None,
) -> str:
    if node.requires_event and node.requires_event not in state.fired_events:
        return f"需要先触发事件 '{node.requires_event}'"
    if node.requires_event_pack and storyline is not None:
        if not _pack_fired(node.requires_event_pack, storyline, state):
            return f"需要先触发事件包 '{node.requires_event_pack}' 中的事件"
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
