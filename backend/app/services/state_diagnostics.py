"""Consistency checks for authoritative campaign state.

Diagnostics answer a narrow question: could the stored state be impossible under
the authored world? They never mutate anything by themselves; the repair path
builds a new state explicitly and records what it changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.models.game import GameState, WorldDefinition
from app.models.knowledge import Storyline

ERROR = "error"
WARNING = "warning"


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    repair: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "repair": self.repair,
        }


@dataclass
class RepairResult:
    state: GameState
    applied: List[str] = field(default_factory=list)


def check_state(
    world: WorldDefinition,
    state: GameState,
    storyline: Optional[Storyline] = None,
    mode: str = "free",
) -> List[Issue]:
    issues: List[Issue] = []
    location_ids = {loc.id for loc in world.locations}
    item_ids = {item.id for item in world.items}
    door_ids = {door.id for door in world.doors}
    quest_map = {quest.id: quest for quest in world.quests}
    event_ids = {event.id for event in world.events}

    if state.location_id not in location_ids:
        issues.append(
            Issue(
                ERROR,
                "unknown_location",
                f"player is at unknown location '{state.location_id}'",
                f"move player to start location '{world.start_location_id}'",
            )
        )

    for item_id, count in state.inventory.items():
        if item_id not in item_ids:
            issues.append(
                Issue(
                    WARNING,
                    "unknown_inventory_item",
                    f"inventory holds undefined item '{item_id}'",
                    "remove the undefined item",
                )
            )
        if count < 0:
            issues.append(
                Issue(
                    ERROR,
                    "negative_inventory",
                    f"inventory item '{item_id}' has negative count {count}",
                    "clamp to zero",
                )
            )
        placed = state.item_locations.get(item_id)
        if placed and count > 0:
            issues.append(
                Issue(
                    WARNING,
                    "item_both_carried_and_placed",
                    f"item '{item_id}' is carried and also placed at '{placed}'",
                    "clear its placed location",
                )
            )
        if not placed and count <= 0:
            issues.append(
                Issue(
                    WARNING,
                    "item_lost",
                    f"item '{item_id}' is neither carried nor placed anywhere",
                    None,
                )
            )

    for item_id, place in state.item_locations.items():
        if item_id not in item_ids:
            issues.append(
                Issue(
                    WARNING,
                    "placed_unknown_item",
                    f"undefined item '{item_id}' is placed at '{place}'",
                    "drop the placement entry",
                )
            )
        if place not in location_ids:
            issues.append(
                Issue(
                    ERROR,
                    "item_in_unknown_location",
                    f"item '{item_id}' is placed at unknown location '{place}'",
                    "move to start location",
                )
            )

    for item in world.items:
        if (
            item.id not in state.inventory
            and item.id not in state.item_locations
        ):
            issues.append(
                Issue(
                    WARNING,
                    "unplaced_item",
                    f"item '{item.id}' from the world is not placed anywhere",
                    None,
                )
            )

    for door_id, opened in state.doors.items():
        if door_id not in door_ids:
            issues.append(
                Issue(
                    WARNING,
                    "unknown_door_state",
                    f"state tracks undefined door '{door_id}'",
                    "drop the door entry",
                )
            )
    for door in world.doors:
        if door.id not in state.doors:
            issues.append(
                Issue(
                    WARNING,
                    "missing_door_state",
                    f"door '{door.id}' has no explicit state (falls back to its definition)",
                    f"initialise to {door.opened}",
                )
            )

    for quest_id, progress in state.quests.items():
        quest = quest_map.get(quest_id)
        if quest is None:
            issues.append(
                Issue(
                    WARNING,
                    "unknown_quest_progress",
                    f"progress exists for undefined quest '{quest_id}'",
                    "drop the progress entry",
                )
            )
            continue
        objective_ids = {objective.id for objective in quest.objectives}
        for objective_id, count in progress.objectives.items():
            if objective_id not in objective_ids:
                issues.append(
                    Issue(
                        WARNING,
                        "unknown_objective",
                        f"quest '{quest_id}' has progress for unknown objective '{objective_id}'",
                        "drop the objective progress",
                    )
                )
            if count < 0:
                issues.append(
                    Issue(
                        ERROR,
                        "negative_objective",
                        f"quest '{quest_id}' objective '{objective_id}' has negative progress {count}",
                        "clamp to zero",
                    )
                )
        if progress.status == "completed":
            unmet = [
                objective.id
                for objective in quest.objectives
                if progress.objectives.get(objective.id, 0) < objective.required_count
            ]
            if unmet:
                issues.append(
                    Issue(
                        WARNING,
                        "completed_but_incomplete",
                        f"quest '{quest_id}' is completed but objectives {unmet} are unmet",
                        None,
                    )
                )

    for quest in world.quests:
        if quest.id not in state.quests:
            issues.append(
                Issue(
                    WARNING,
                    "missing_quest_progress",
                    f"quest '{quest.id}' has no progress row",
                    "initialise progress",
                )
            )

    for event_id in state.fired_events:
        if event_id not in event_ids:
            issues.append(
                Issue(
                    WARNING,
                    "unknown_fired_event",
                    f"fired event '{event_id}' is not defined in the world",
                    "drop the record",
                )
            )

    if mode == "storyline" and storyline is not None:
        node_map = {node.id: node for node in storyline.nodes}
        for node_id in state.story_nodes:
            if node_id not in node_map:
                issues.append(
                    Issue(
                        WARNING,
                        "unknown_story_node",
                        f"entered story node '{node_id}' is not in the storyline",
                        "drop the record",
                    )
                )
        for node in storyline.nodes:
            if node.id not in state.story_nodes:
                continue
            missing = [
                prereq
                for prereq in node.prereq
                if prereq not in state.story_nodes
            ]
            if missing:
                issues.append(
                    Issue(
                        WARNING,
                        "story_node_out_of_order",
                        f"story node '{node.id}' entered before prerequisites {missing}",
                        None,
                    )
                )
    return issues


def has_blocking_issues(issues: List[Issue]) -> bool:
    return any(issue.severity == ERROR for issue in issues)


def repair_state(
    world: WorldDefinition,
    state: GameState,
    issues: Optional[List[Issue]] = None,
    storyline: Optional[Storyline] = None,
) -> RepairResult:
    """Apply the conservative repairs advertised by diagnostics.

    Only unambiguous fixes are applied: dangling locations move to the start,
    negative counts are clamped, duplicate placements are cleared and missing
    door/quest rows are initialised from configuration.
    """
    issues = issues if issues is not None else check_state(world, state)
    repaired = state.model_copy(deep=True)
    applied: List[str] = []
    location_ids = {loc.id for loc in world.locations}
    item_ids = {item.id for item in world.items}
    door_ids = {door.id for door in world.doors}
    quest_map = {quest.id: quest for quest in world.quests}
    event_ids = {event.id for event in world.events}
    story_node_ids = {node.id for node in storyline.nodes} if storyline else set()
    for issue in issues:
        if issue.code == "unknown_location":
            repaired.location_id = world.start_location_id
            applied.append("unknown_location -> start_location_id")
        elif issue.code == "negative_inventory":
            for item_id, count in list(repaired.inventory.items()):
                if count < 0:
                    repaired.inventory[item_id] = 0
                    applied.append(f"negative_inventory:{item_id} -> 0")
        elif issue.code == "item_both_carried_and_placed":
            for item_id, count in repaired.inventory.items():
                if count > 0 and item_id in repaired.item_locations:
                    del repaired.item_locations[item_id]
                    applied.append(f"placement cleared:{item_id}")
        elif issue.code == "placed_unknown_item":
            for item_id in list(repaired.item_locations):
                if item_id not in item_ids:
                    del repaired.item_locations[item_id]
                    applied.append(f"dropped placement:{item_id}")
        elif issue.code == "item_in_unknown_location":
            for item_id, place in list(repaired.item_locations.items()):
                if place not in location_ids:
                    repaired.item_locations[item_id] = world.start_location_id
                    applied.append(f"placement:{item_id} -> start")
        elif issue.code == "unknown_door_state":
            for door_id in list(repaired.doors):
                if door_id not in door_ids:
                    del repaired.doors[door_id]
                    applied.append(f"dropped door state:{door_id}")
        elif issue.code == "missing_door_state":
            for door in world.doors:
                if door.id not in repaired.doors:
                    repaired.doors[door.id] = door.opened
                    applied.append(f"initialised door:{door.id}")
        elif issue.code == "unknown_inventory_item":
            for item_id in list(repaired.inventory):
                if item_id not in item_ids:
                    del repaired.inventory[item_id]
                    applied.append(f"dropped inventory item:{item_id}")
        elif issue.code == "unknown_fired_event":
            repaired.fired_events = [
                event_id for event_id in repaired.fired_events if event_id in event_ids
            ]
            applied.append("dropped unknown fired events")
        elif issue.code == "unknown_story_node" and story_node_ids:
            repaired.story_nodes = [
                node_id for node_id in repaired.story_nodes if node_id in story_node_ids
            ]
            applied.append("dropped unknown story nodes")
        elif issue.code == "unknown_quest_progress":
            for quest_id in list(repaired.quests):
                if quest_id not in quest_map:
                    del repaired.quests[quest_id]
                    applied.append(f"dropped quest progress:{quest_id}")
        elif issue.code == "unknown_objective":
            for quest_id, progress in repaired.quests.items():
                quest = quest_map.get(quest_id)
                if quest is None:
                    continue
                known = {objective.id for objective in quest.objectives}
                for objective_id in list(progress.objectives):
                    if objective_id not in known:
                        del progress.objectives[objective_id]
                        applied.append(
                            f"dropped objective:{quest_id}/{objective_id}"
                        )
        elif issue.code == "negative_objective":
            for quest_id, progress in repaired.quests.items():
                for objective_id, count in list(progress.objectives.items()):
                    if count < 0:
                        progress.objectives[objective_id] = 0
                        applied.append(
                            f"negative_objective:{quest_id}/{objective_id} -> 0"
                        )
        elif issue.code == "missing_quest_progress":
            from app.engines.quest_engine import initial_quest_progress

            for quest_id, progress in initial_quest_progress(world).items():
                if quest_id not in repaired.quests:
                    repaired.quests[quest_id] = progress
                    applied.append(f"initialised quest:{quest_id}")

    if applied:
        repaired.updated_at = state.updated_at
    return RepairResult(state=repaired, applied=applied)