"""Persistence tests for authored knowledge, play config and model presets."""
import pytest

from app.db.scenario_store import ScenarioStore
from app.models.knowledge import (
    KnowledgeEntryCreate,
    KnowledgeEntryPatch,
    ModelPresetCreate,
    ModelPresetPatch,
    PlayConfig,
    Storyline,
    StoryNode,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "knowledge_store.db")


@pytest.fixture
def store(db_path):
    with ScenarioStore(db_path) as s:
        yield s


@pytest.fixture
def scenario(store):
    return store.create_scenario(title="雾港", language="zh")


# ------------------------------------------------------------- knowledge CRUD
def test_knowledge_roundtrip_keeps_tags_and_order(store, scenario):
    store.add_knowledge(
        scenario.id,
        KnowledgeEntryCreate(
            layer="scene",
            title="码头",
            content="潮湿的木板和咸味。",
            tags=["地点", "海边"],
            depth=3,
            sort_order=2,
        ),
    )
    store.add_knowledge(
        scenario.id,
        KnowledgeEntryCreate(layer="global", title="世界总览", content="雾港群岛", depth=1),
    )

    entries = store.list_knowledge(scenario.id)
    assert [entry.title for entry in entries] == ["世界总览", "码头"]
    assert entries[1].tags == ["地点", "海边"]
    assert entries[1].depth == 3
    assert entries[1].scenario_id == scenario.id


def test_knowledge_patch_and_delete(store, scenario):
    entry = store.add_knowledge(
        scenario.id, KnowledgeEntryCreate(layer="event", title="小事件", content="猫")
    )
    updated = store.update_knowledge(
        entry.id,
        KnowledgeEntryPatch(always_on=True, mode_scope="storyline", content="黑猫经过"),
    )
    assert updated.always_on is True
    assert updated.mode_scope == "storyline"
    assert updated.content == "黑猫经过"

    assert store.delete_knowledge(entry.id) is True
    assert store.get_knowledge(entry.id) is None
    assert store.delete_knowledge(entry.id) is False


def test_include_disabled_filters_entries(store, scenario):
    store.add_knowledge(
        scenario.id, KnowledgeEntryCreate(layer="story", title="停用", enabled=False)
    )
    store.add_knowledge(scenario.id, KnowledgeEntryCreate(layer="story", title="启用"))
    assert len(store.list_knowledge(scenario.id)) == 2
    assert [e.title for e in store.list_knowledge(scenario.id, include_disabled=False)] == [
        "启用"
    ]


def test_knowledge_is_scoped_to_its_scenario(store, scenario):
    other = store.create_scenario(title="另一个", language="zh")
    store.add_knowledge(scenario.id, KnowledgeEntryCreate(layer="scene", title="只有这里"))
    assert store.list_knowledge(other.id) == []


# ---------------------------------------------------------------- play config
def test_default_play_config_is_free_with_full_depth(store, scenario):
    config = store.get_play_config(scenario.id)
    assert config.scenario_id == scenario.id
    assert config.mode == "free"
    assert config.max_depth == 9
    assert config.storyline.nodes == []


def test_play_config_upsert_roundtrips_storyline(store, scenario):
    config = PlayConfig(
        scenario_id=scenario.id,
        mode="storyline",
        max_depth=4,
        model_preset_id="builtin-fast",
        storyline=Storyline(
            name="主线",
            nodes=[StoryNode(id="arrive", title="抵达")],
        ),
    )
    stored = store.set_play_config(scenario.id, config)
    assert stored.mode == "storyline"
    assert stored.max_depth == 4
    assert stored.model_preset_id == "builtin-fast"

    again = store.get_play_config(scenario.id)
    assert [node.id for node in again.storyline.nodes] == ["arrive"]
    assert again.storyline.name == "主线"


# --------------------------------------------------------------- model presets
def test_builtin_presets_are_seeded_once(store, scenario, db_path):
    presets = store.list_model_presets()
    ids = [preset.id for preset in presets]
    assert ids[:4] == [
        "builtin-balanced",
        "builtin-grim",
        "builtin-fast",
        "builtin-intent",
    ]
    assert all(preset.is_builtin for preset in presets)

    with ScenarioStore(db_path) as reopened:
        # Re-seeding a live database is an idempotent no-op.
        assert len(reopened.list_model_presets()) == 4


def test_custom_preset_lifecycle(store, scenario):
    preset = store.add_model_preset(
        ModelPresetCreate(name="实验叙事", role="narrator", temperature=1.1, max_tokens=640)
    )
    assert preset.is_builtin is False
    assert preset.provider == ""  # empty provider = follow the global router

    updated = store.update_model_preset(preset.id, ModelPresetPatch(max_tokens=320))
    assert updated.max_tokens == 320
    assert store.delete_model_preset(preset.id) is True
    assert store.get_model_preset(preset.id) is None


def test_builtin_preset_cannot_be_deleted(store, scenario):
    assert store.delete_model_preset("builtin-balanced") is False
    assert store.get_model_preset("builtin-balanced") is not None


# ------------------------------------------------------------ scenario cleanup
def test_deleting_a_scenario_removes_its_knowledge_and_config(store, scenario):
    store.add_knowledge(
        scenario.id, KnowledgeEntryCreate(layer="global", title="世界")
    )
    store.set_play_config(
        scenario.id, PlayConfig(scenario_id=scenario.id, mode="storyline")
    )

    assert store.delete_scenario(scenario.id) is True
    assert store.list_knowledge(scenario.id) == []
    assert store.get_play_config(scenario.id).mode == "free"


def test_schema_version_includes_knowledge_tables(tmp_path):
    with ScenarioStore(str(tmp_path / "migrated.db")) as store:
        version = store._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == ScenarioStore.SCHEMA_VERSION == 7
        names = {
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"scenario_knowledge", "scenario_play_config", "model_presets"} <= names