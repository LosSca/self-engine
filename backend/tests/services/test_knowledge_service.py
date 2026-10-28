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