"""Validated configuration for world knowledge, play modes and model presets.

Knowledge is *authored data*, never executable: every field is checked by
Pydantic and unknown keys are rejected (extra="forbid"). The play mode is a
first-class, persisted setting because it changes which configuration is
authoritative at run time:

* storyline - the configured main/branch nodes must be followed; the engine
  records which nodes have been entered in the authoritative state.
* free      - no story nodes are entered; storyline-only knowledge is not
  injected, but always-on small events stay available.

The LLM only ever *reads* this configuration through a rendered context. It can
neither add entries nor mark nodes as reached.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.game import Condition

KnowledgeLayer = Literal["global", "story", "scene", "character", "event"]
ModeScope = Literal["both", "storyline", "free"]
PlayMode = Literal["storyline", "free"]
PresetRole = Literal["narrator", "intent", "judge", "dialogue", "summary", "any"]

#: Prompt assembly order: stable world facts first, volatile events last.
LAYER_ORDER: List[str] = ["global", "story", "scene", "character", "event"]

LAYER_LABELS: Dict[str, str] = {
    "global": "世界总览",
    "story": "故事线",
    "scene": "场景",
    "character": "人物",
    "event": "事件",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeEntryCreate(_Strict):
    """One authored knowledge entry, injected by depth chain."""

    layer: KnowledgeLayer
    title: str = Field(..., min_length=1, max_length=200)
    content: str = ""
    tags: List[str] = Field(default_factory=list)
    #: 0 is core knowledge, 9 is the most peripheral detail. The context builder
    #: keeps entries whose depth is <= max_depth (always_on entries bypass this).
    depth: int = Field(5, ge=0, le=9)
    #: Always injected, even in free mode and past the depth cutoff.
    always_on: bool = False
    #: Both modes, storyline only, or free only.
    mode_scope: ModeScope = "both"
    enabled: bool = True
    sort_order: int = 0
    #: NPC ids this entry is private to. Empty means every scene may load it;
    #: otherwise it is only injected when one of them is present or targeted.
    audience_npc_ids: List[str] = Field(default_factory=list)


class KnowledgeEntry(KnowledgeEntryCreate):
    id: str
    scenario_id: str
    created_at: str = ""


class KnowledgeEntryPatch(_Strict):
    layer: Optional[KnowledgeLayer] = None
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    depth: Optional[int] = Field(None, ge=0, le=9)
    always_on: Optional[bool] = None
    mode_scope: Optional[ModeScope] = None
    enabled: Optional[bool] = None
    sort_order: Optional[int] = None
    audience_npc_ids: Optional[List[str]] = None


StoryKind = Literal["main", "branch", "side"]


class StoryLineDef(_Strict):
    """One named line of story nodes.

    The three kinds describe how a line attaches to the rest of the story:

    * main   - the trunk; every scenario has exactly one, entered in order.
    * branch - hangs off the trunk (or another branch): it can only be entered
      after its prerequisites, so it belongs to a specific point in the story.
    * side   - an independent line: it has no prerequisites of its own and
      starts from its own trigger (conditions or a required event), so it runs
      beside the trunk instead of hanging off it.

    Grouping is editorial bookkeeping: it never changes progression (the
    prerequisite graph still decides), it only lets an author see and switch a
    whole line at once.
    """

    id: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    kind: StoryKind = "main"
    description: str = ""
    #: A disabled line keeps its nodes configured but the engine never enters
    #: them. This is an editorial switch, never a hint sent to the model.
    enabled: bool = True


class StoryNode(_Strict):
    """One node of a configured storyline.

    A node becomes *entered* when every prerequisite node has been entered and
    its conditions hold against the authoritative state. Nothing else (and in
    particular no model output) can enter a node.
    """

    id: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    kind: StoryKind = "main"
    #: Optional line this node belongs to (see Storyline.lines). Empty means the
    #: node is ungrouped, which stays legal for storylines authored before
    #: lines existed.
    line: str = ""
    summary: str = ""
    prereq: List[str] = Field(default_factory=list)
    conditions: List[Condition] = Field(default_factory=list)
    #: Event id that must have fired for this node to be entered.
    requires_event: Optional[str] = None
    #: Knowledge entries that become relevant once the node is entered.
    knowledge_ids: List[str] = Field(default_factory=list)


class Storyline(_Strict):
    name: str = ""
    description: str = ""
    nodes: List[StoryNode] = Field(default_factory=list)
    #: Optional named lines the nodes are grouped into.
    lines: List[StoryLineDef] = Field(default_factory=list)

    def node_map(self) -> Dict[str, StoryNode]:
        return {node.id: node for node in self.nodes}

    def line_map(self) -> Dict[str, StoryLineDef]:
        return {line.id: line for line in self.lines}

    def roots(self) -> List[StoryNode]:
        return [node for node in self.nodes if not node.prereq]


class PlayConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    scenario_id: str = ""
    mode: PlayMode = "free"
    storyline: Storyline = Field(default_factory=Storyline)
    #: Knowledge depth chain cutoff (0 = only core knowledge).
    max_depth: int = Field(9, ge=0, le=9)
    #: Maximum injected title+content size for one narration (0 = no limit).
    context_budget_chars: int = Field(0, ge=0, le=200000)
    model_preset_id: str = ""
    #: Optional preset used by the intent advisor (role intent/any).
    intent_preset_id: str = ""
    #: Optional preset used when narrating a conversation (dialogue/any).
    dialogue_preset_id: str = ""
    #: Optional preset used to compress long-term memory (summary/any).
    summary_preset_id: str = ""
    #: Extra system instructions for narration (rules still bind the outcome).
    narrator_prompt: str = ""
    updated_at: str = ""


class PlayConfigUpdate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    mode: PlayMode
    storyline: Optional[Storyline] = None
    max_depth: Optional[int] = Field(None, ge=0, le=9)
    context_budget_chars: Optional[int] = Field(None, ge=0, le=200000)
    model_preset_id: Optional[str] = None
    intent_preset_id: Optional[str] = None
    dialogue_preset_id: Optional[str] = None
    summary_preset_id: Optional[str] = None
    narrator_prompt: Optional[str] = Field(None, max_length=4000)


class ModelPresetCreate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: str = Field(..., min_length=1, max_length=120)
    role: PresetRole = "narrator"
    provider: str = ""
    model: str = ""
    temperature: float = Field(0.8, ge=0.0, le=2.0)
    max_tokens: int = Field(800, ge=1, le=32768)
    notes: str = ""


class ModelPresetPatch(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: Optional[str] = Field(None, min_length=1, max_length=120)
    role: Optional[PresetRole] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(None, ge=1, le=32768)
    notes: Optional[str] = None


class ModelPreset(ModelPresetCreate):
    id: str
    is_builtin: bool = False
    created_at: str = ""


class KnowledgeBlock(BaseModel):
    entry_id: str
    layer: str
    title: str
    content: str
    tags: List[str] = Field(default_factory=list)
    depth: int = 5


class ExcludedEntry(BaseModel):
    entry_id: str
    title: str
    reason: str


class ContextStats(BaseModel):
    mode: str = "free"
    total: int = 0
    included: int = 0
    excluded: List[ExcludedEntry] = Field(default_factory=list)
    layer_counts: Dict[str, int] = Field(default_factory=dict)
    max_depth: int = 9


class KnowledgeContext(BaseModel):
    scenario_id: str = ""
    mode: str = "free"
    blocks: List[KnowledgeBlock] = Field(default_factory=list)
    stats: ContextStats = Field(default_factory=ContextStats)
    #: The active trunk node (the last main node entered), kept for compatibility.
    story_node: Optional[str] = None
    #: Every node whose knowledge was boosted for this context, so a debugger can
    #: tell why an entry ranked first.
    story_nodes: List[str] = Field(default_factory=list)

    def prompt_blocks(self) -> List[Dict[str, Any]]:
        """Plain dicts for the narrator prompt (no behaviour attached)."""
        return [
            {
                "id": block.entry_id,
                "layer": block.layer,
                "title": block.title,
                "content": block.content,
            }
            for block in self.blocks
        ]


class ScenarioPlayContext(BaseModel):
    """Everything the action pipeline needs to know about authored context."""

    model_config = ConfigDict(protected_namespaces=())

    scenario_id: str = ""
    language: str = ""
    config: PlayConfig = Field(default_factory=PlayConfig)
    entries: List[KnowledgeEntry] = Field(default_factory=list)
    preset: Optional[ModelPreset] = None
    intent_preset: Optional[ModelPreset] = None
    dialogue_preset: Optional[ModelPreset] = None
    summary_preset: Optional[ModelPreset] = None
