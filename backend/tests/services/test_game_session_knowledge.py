"""Live narration loop must receive authored world knowledge (m01005).

The deterministic v1 action service already injects knowledge; this covers the
legacy streaming path used by real play: the knowledge is assembled from the
scenario store and passed to GameSession as background context only.
"""
from __future__ import annotations

import pytest

from app.db.scenario_store import ScenarioStore
from app.models.knowledge import KnowledgeEntryCreate
from app.services.game_session import GameSession


@pytest.fixture()
def scenario_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "scenarios.db")
    monkeypatch.setenv("SCENARIO_DB_PATH", db_path)
    store = ScenarioStore(db_path)
    scenario = store.create_scenario(title="仓库与钥匙")
    campaign = store.create_campaign(scenario.id, "旅人")
    yield store, scenario, campaign
    store.close()


def _session_with(knowledge: str, provider=None) -> GameSession:
    """A bare session is enough: _with_knowledge only reads two attributes."""
    session = GameSession.__new__(GameSession)
    session._knowledge_context = knowledge
    session._knowledge_provider = provider
    return session


def test_with_knowledge_is_noop_without_knowledge():
    session = _session_with("")
    assert session._with_knowledge("CARDS") == "CARDS"


def test_with_knowledge_appends_background_block():
    session = _session_with("[世界总览] 世界总览\n海风终年不改。")
    merged = session._with_knowledge("CARDS")
    assert merged.startswith("CARDS")
    assert "AUTHORED WORLD KNOWLEDGE" in merged
    assert "background only" in merged
    assert "海风终年不改。" in merged
    assert merged.index("CARDS") < merged.index("世界总览")


def test_with_knowledge_starts_block_when_no_story_cards():
    session = _session_with("[事件] 门口的猫\n一只猫。")
    merged = session._with_knowledge("")
    assert merged.startswith("AUTHORED WORLD KNOWLEDGE")
    assert "门口的猫" in merged


def test_provider_refreshes_knowledge_each_turn():
    edits = {"text": "[世界总览] 旧版本\n一。"}
    session = _session_with("", provider=lambda: edits["text"])
    assert "旧版本" in session._with_knowledge("")
    edits["text"] = "[世界总览] 新版本\n二。"
    merged = session._with_knowledge("")
    assert "新版本" in merged
    assert "旧版本" not in merged


def test_provider_failure_falls_back_to_snapshot():
    def boom():
        raise RuntimeError("store offline")

    session = _session_with("[世界总览] 快照\n内容。", provider=boom)
    merged = session._with_knowledge("CARDS")
    assert "快照" in merged


def test_knowledge_context_loader_formats_enabled_entries(scenario_db, monkeypatch):
    store, scenario, campaign = scenario_db
    store.add_knowledge(
        scenario.id,
        KnowledgeEntryCreate(
            layer="global",
            title="世界总览",
            content="盐价由港口的秤房决定。",
            tags=["world"],
            depth=1,
            always_on=True,
        ),
    )
    store.add_knowledge(
        scenario.id,
        KnowledgeEntryCreate(
            layer="story",
            title="仓库的账本",
            content="隐藏账目。",
            mode_scope="storyline",
            depth=3,
        ),
    )
    store.add_knowledge(
        scenario.id,
        KnowledgeEntryCreate(
            layer="scene",
            title="停用的线索",
            content="不该出现。",
            enabled=False,
        ),
    )

    from app.api import routes_game

    text = routes_game._load_knowledge_context_for_campaign(campaign.id)
    assert "[世界总览] 世界总览" in text
    assert "盐价由港口的秤房决定。" in text
    # Free mode (default play config) drops storyline-only knowledge.
    assert "仓库的账本" not in text
    # Disabled entries never reach the narrator.
    assert "停用的线索" not in text


def test_knowledge_context_loader_returns_empty_for_unknown_campaign(scenario_db):
    from app.api import routes_game

    assert routes_game._load_knowledge_context_for_campaign("nope") == ""
