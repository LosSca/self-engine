"""Tests for the knowledge depth chain (see app/services/knowledge_service.py)."""
import pytest

from app.db.scenario_store import ScenarioStore
from app.models.knowledge import (
    KnowledgeEntry,
    KnowledgeEntryCreate,
    PlayConfig,
    Storyline,
    StoryNode,
)
from app.services import knowledge_service


def _entry(entry_id, layer="scene", depth=5, always_on=False, mode_scope="both",
           enabled=True, sort_order=0, title=None, content="内容", tags=None):
    return KnowledgeEntry(
        id=entry_id,
        scenario_id="sc-1",
        layer=layer,
        title=title or entry_id,
        content=content,
        tags=tags or [],
        depth=depth,
        always_on=always_on,
        mode_scope=mode_scope,
        enabled=enabled,
        sort_order=sort_order,
        created_at="2026-01-01T00:00:00",
    )


@pytest.fixture
def store(tmp_path):
    with ScenarioStore(str(tmp_path / "knowledge.db")) as s:
        yield s


@pytest.fixture
def scenario(store):
    return store.create_scenario(title="雾港", language="zh")


# ---------------------------------------------------------------- depth chain
def test_depth_cutoff_excludes_deep_entries_with_readable_reason():
    entries = [
        _entry("shallow", depth=2),
        _entry("deep", depth=8, title="深层设定"),
    ]
    context = knowledge_service.build_context(entries, mode="storyline", max_depth=5)

    assert [b.entry_id for b in context.blocks] == ["shallow"]
    assert context.stats.excluded[0].title == "深层设定"
    assert "depth 8 > 5" in context.stats.excluded[0].reason
    assert context.stats.max_depth == 5


def test_always_on_bypasses_the_depth_cutoff():
    entries = [_entry("deep", depth=9, always_on=True)]
    context = knowledge_service.build_context(entries, mode="storyline", max_depth=0)
    assert [b.entry_id for b in context.blocks] == ["deep"]


def test_mode_scope_filters_both_directions():
    entries = [
        _entry("shared", mode_scope="both"),
        _entry("story_only", mode_scope="storyline"),
        _entry("free_only", mode_scope="free"),
    ]
    free = knowledge_service.build_context(entries, mode="free")
    # Same layer and depth -> stable alphabetical tie-break on the title.
    assert [b.entry_id for b in free.blocks] == ["free_only", "shared"]
    assert "自由模式不载入" in free.stats.excluded[0].reason

    story = knowledge_service.build_context(entries, mode="storyline")
    assert [b.entry_id for b in story.blocks] == ["shared", "story_only"]


def test_free_mode_keeps_always_on_small_events():
    """User requirement: 自由模式下小事件仍然存在，一直在世界知识内保持开启。"""
    entries = [
        _entry("ambient", layer="event", mode_scope="storyline", always_on=True),
        _entry("plot_only", layer="story", mode_scope="storyline"),
    ]
    context = knowledge_service.build_context(entries, mode="free")
    assert [b.entry_id for b in context.blocks] == ["ambient"]
    assert context.stats.excluded[0].entry_id == "plot_only"


def test_ordering_boosts_the_active_story_node_then_layers():
    storyline = Storyline(
        name="主线",
        nodes=[
            StoryNode(id="n1", title="第一节", knowledge_ids=["char1"]),
        ],
    )
    entries = [
        _entry("scene1", layer="scene", depth=1),
        _entry("global1", layer="global", depth=9),
        _entry("char1", layer="character", depth=9),
    ]
    context = knowledge_service.build_context(
        entries, mode="storyline", storyline=storyline, story_node="n1"
    )
    assert [b.entry_id for b in context.blocks] == ["char1", "global1", "scene1"]
    assert context.story_node == "n1"
    assert context.story_nodes == ["n1"]


def test_all_entered_nodes_boost_knowledge_without_boosting_future_nodes():
    storyline = Storyline(nodes=[
        StoryNode(id="old", title="Earlier main", knowledge_ids=["old", "shared"]),
        StoryNode(id="current", title="Current main", prereq=["old"], knowledge_ids=["current"]),
        StoryNode(id="branch", title="Branch", kind="branch", prereq=["old"],
                  knowledge_ids=["branch", "shared"]),
        StoryNode(id="side", title="Side", kind="side", requires_event="keeper_alert",
                  knowledge_ids=["side", "shared"]),
        StoryNode(id="future", title="Future", prereq=["current"], knowledge_ids=["future"]),
    ])
    entries = [_entry("global", layer="global"), _entry("future", depth=0)] + [
        _entry(entry_id, sort_order=index)
        for index, entry_id in enumerate(["old", "current", "branch", "side", "shared"])
    ]
    entered = ["old", "current", "branch", "side"]
    context = knowledge_service.build_context(
        entries, mode="storyline", storyline=storyline,
        story_node="current", story_nodes=entered,
    )
    assert [b.entry_id for b in context.blocks] == [
        "old", "current", "branch", "side", "shared", "global", "future",
    ]
    assert context.story_node == "current"
    assert context.story_nodes == entered
    assert context.stats.included == len(entries)


@pytest.mark.parametrize("overrides", [
    {"depth": 9}, {"enabled": False}, {"mode_scope": "free"},
])
def test_node_boost_does_not_bypass_entry_filters(overrides):
    storyline = Storyline(nodes=[StoryNode(id="n1", title="Main", knowledge_ids=["hidden"])])
    context = knowledge_service.build_context(
        [_entry("visible"), _entry("hidden", **overrides)],
        mode="storyline", max_depth=5, storyline=storyline, story_nodes=["n1"],
    )
    assert [b.entry_id for b in context.blocks] == ["visible"]
    assert [e.entry_id for e in context.stats.excluded] == ["hidden"]


@pytest.mark.parametrize("node_args", [
    {"story_node": "n1"},
    {"story_nodes": ["n1", "side"]},
    {"story_node": "n1", "story_nodes": ["n1", "side"]},
])
def test_free_mode_ignores_stale_story_nodes_including_legacy_node(node_args):
    storyline = Storyline(nodes=[
        StoryNode(id="n1", title="Main", knowledge_ids=["character"]),
        StoryNode(id="side", title="Side", kind="side", requires_event="keeper_alert",
                  knowledge_ids=["ambient", "story_only"]),
    ])
    entries = [
        _entry("global", layer="global"),
        _entry("character", layer="character"),
        _entry("ambient", layer="event", always_on=True, mode_scope="storyline"),
        _entry("story_only", layer="story", mode_scope="storyline"),
    ]
    baseline = knowledge_service.build_context(entries, mode="free", storyline=storyline)
    context = knowledge_service.build_context(
        entries, mode="free", storyline=storyline, **node_args,
    )
    assert [b.entry_id for b in context.blocks] == ["global", "character", "ambient"]
    assert context.model_dump() == baseline.model_dump()
    assert context.story_node is None
    assert context.story_nodes == []


def test_disabled_entries_are_never_injected():
    entries = [_entry("off", enabled=False)]
    context = knowledge_service.build_context(entries, mode="free")
    assert context.blocks == []
    assert context.stats.excluded[0].reason == "条目已停用"


def test_stats_count_layers_and_prompt_blocks_are_plain_data():
    entries = [_entry("g", layer="global"), _entry("s", layer="scene")]
    context = knowledge_service.build_context(entries, mode="free", scenario_id="sc-1")

    assert context.stats.total == 2
    assert context.stats.included == 2
    assert context.stats.layer_counts["global"] == 1
    assert context.stats.layer_counts["character"] == 0
    blocks = context.prompt_blocks()
    assert blocks[0] == {
        "id": "g",
        "layer": "global",
        "title": "g",
        "content": "内容",
    }


# ------------------------------------------------------------ play validation
def test_storyline_mode_without_nodes_is_rejected():
    config = PlayConfig(scenario_id="sc-1", mode="storyline")
    errors = knowledge_service.validate_play_config(config, [])
    assert "故事线模式至少需要一个故事线节点" in errors


def test_storyline_unknown_prereq_and_knowledge_are_reported():
    config = PlayConfig(
        scenario_id="sc-1",
        mode="storyline",
        storyline=Storyline(
            name="主线",
            nodes=[
                StoryNode(id="n1", title="一"),
                StoryNode(id="n2", title="二", prereq=["nope"]),
                StoryNode(id="n3", title="三", knowledge_ids=["missing-knowledge"]),
            ],
        ),
    )
    errors = knowledge_service.validate_play_config(config, [])
    assert any("nope" in e for e in errors)
    assert any("missing-knowledge" in e for e in errors)


# ------------------------------------------------------------- stored context
def test_load_play_context_reads_config_and_entries(store, scenario):
    store.add_knowledge(
        scenario.id, KnowledgeEntryCreate(layer="global", title="世界", content="雾")
    )
    store.set_play_config(
        scenario.id, PlayConfig(scenario_id=scenario.id, mode="storyline")
    )

    play = knowledge_service.load_play_context(store, scenario.id)
    assert play is not None
    assert play.config.mode == "storyline"
    assert [entry.title for entry in play.entries] == ["世界"]

    assert knowledge_service.load_play_context(store, "nope") is None
    assert knowledge_service.load_play_context(store, "") is None

def test_focus_tags_rank_related_entries_first():
    entries = [
        _entry("elsewhere", layer="scene", depth=2, tags=["harbor"]),
        _entry("here", layer="scene", depth=2, tags=["tavern", "npc"]),
    ]
    plain = knowledge_service.build_context(entries, mode="free")
    focused = knowledge_service.build_context(entries, mode="free", focus=["Tavern"])
    assert [b.entry_id for b in plain.blocks] == ["elsewhere", "here"]
    assert [b.entry_id for b in focused.blocks] == ["here", "elsewhere"]


def test_budget_truncates_and_reports_the_overflow():
    entries = [
        _entry("first", layer="global", depth=1, title="A", content="x" * 10),
        _entry("second", layer="global", depth=2, title="B", content="y" * 30),
    ]
    context = knowledge_service.build_context(entries, mode="free", budget_chars=15)
    assert [b.entry_id for b in context.blocks] == ["first"]
    assert context.stats.excluded[0].entry_id == "second"
    assert "预算" in context.stats.excluded[0].reason
    unlimited = knowledge_service.build_context(entries, mode="free", budget_chars=0)
    assert [b.entry_id for b in unlimited.blocks] == ["first", "second"]


def test_audience_entries_need_their_npc_in_focus():
    entries = [
        _entry("public", layer="global"),
        _entry("secret", layer="character", title="Guard secret"),
    ]
    entries[1] = entries[1].model_copy(update={"audience_npc_ids": ["guard"], "tags": ["guard"]})

    hidden = knowledge_service.build_context(entries, mode="free")
    assert [b.entry_id for b in hidden.blocks] == ["public"]
    assert hidden.stats.excluded[0].entry_id == "secret"
    assert "NPC" in hidden.stats.excluded[0].reason

    shown = knowledge_service.build_context(entries, mode="free", focus=["guard"])
    assert [b.entry_id for b in shown.blocks] == ["secret", "public"]
    assert not shown.stats.excluded

    elsewhere = knowledge_service.build_context(entries, mode="free", focus=["tavern"])
    assert [b.entry_id for b in elsewhere.blocks] == ["public"]
