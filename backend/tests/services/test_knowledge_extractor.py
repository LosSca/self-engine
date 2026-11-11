import pytest

from app.db.scenario_store import ScenarioStore
from app.models.knowledge import KnowledgeEntryCreate
from app.services import knowledge_extractor


class _Row(dict):
    pass


def _turn(text, request_id="req-1", version=3):
    return {"event_type": "narrator.response", "request_id": request_id, "state_version": version, "payload": {"text": text}}


def test_split_sentences_handles_both_punctuation_styles():
    text = "The mill is quiet. You step inside. \u4ed3\u5e93\u95e8\u9501\u7740\u3002\u4f60\u6253\u5f00\u5b83\u5417\uff1f"
    assert knowledge_extractor.split_sentences(text) == [
        "The mill is quiet.",
        "You step inside.",
        "\u4ed3\u5e93\u95e8\u9501\u7740\u3002",
        "\u4f60\u6253\u5f00\u5b83\u5417\uff1f",
    ]


def test_questions_are_not_canon():
    assert knowledge_extractor.classify_sentence("Do you open it?") is None
    assert knowledge_extractor.classify_sentence("\u4f60\u6253\u5f00\u5b83\u5417\uff1f") is None


def test_classifier_separates_player_and_scene():
    assert knowledge_extractor.classify_sentence("You stand at the crossroads.") == "character"
    assert knowledge_extractor.classify_sentence("The keeper watches the gate.") == "event"
    assert knowledge_extractor.classify_sentence("\u4f60\u62ff\u7740\u4e00\u628a\u94c1\u94a5\u5319\u3002") == "character"
    assert knowledge_extractor.classify_sentence("\u4ed3\u5e93\u95e8\u662f\u9501\u7740\u7684\u3002") == "event"


def test_fact_of_recognises_whole_sentence_facts():
    assert knowledge_extractor.fact_of("The Old Mill is a ruined tower.") == ("Old Mill", "ruined tower")
    assert knowledge_extractor.fact_of("\u4ed3\u5e93\u95e8\u662f\u9501\u7740\u7684\u3002") == ("\u4ed3\u5e93\u95e8", "\u9501\u7740\u7684")
    assert knowledge_extractor.fact_of("You walk slowly through the fog.") is None


def test_proposals_are_deterministic_and_deduplicated():
    turns = knowledge_extractor.turn_texts([
        _turn("The Old Mill is a ruined tower. You stand at the crossroads.", "req-1", 2),
        _turn("You stand at the crossroads. The keeper watches the gate.", "req-2", 5),
    ])
    first = knowledge_extractor.proposals_from_turns(turns)
    second = knowledge_extractor.proposals_from_turns(turns)
    assert first == second
    assert [p["content"] for p in first] == [
        "The Old Mill is a ruined tower.",
        "You stand at the crossroads.",
        "The keeper watches the gate.",
    ]
    assert first[0]["is_fact"] is True
    assert first[1]["source_request_id"] == "req-1"
    assert first[2]["source_request_id"] == "req-2"


def test_proposals_carry_their_source_turn():
    turns = knowledge_extractor.turn_texts([_turn("The keeper watches the gate.", "req-9", 7)])
    assert turns[0]["turn"] == 7
    assert knowledge_extractor.proposals_from_turns(turns)[0]["source_turn"] == 7


def test_plan_import_skips_what_is_already_known():
    proposals = [
        {"title": "Old Mill", "content": "The Old Mill is a ruined tower.", "layer": "event"},
        {"title": "Keeper", "content": "The keeper watches the gate.", "layer": "event"},
    ]
    existing = [KnowledgeEntryCreate(layer="event", title="Old Mill", content="The Old Mill is a ruined tower.")]
    entries, warnings = knowledge_extractor.plan_import(proposals, existing)
    assert [e.content for e in entries] == ["The keeper watches the gate."]
    assert warnings == []


def test_plan_import_reports_bad_proposals_without_losing_good_ones():
    proposals = [
        "not an object",
        {"title": "", "content": "", "layer": "event"},
        {"title": "Gate", "content": "The gate is shut.", "layer": "unknown-layer"},
    ]
    entries, warnings = knowledge_extractor.plan_import(proposals)
    assert [e.content for e in entries] == ["The gate is shut."]
    assert entries[0].layer == "event"
    assert len(warnings) == 2


def test_plan_import_gives_duplicate_titles_distinct_names():
    proposals = [
        {"title": "Gate", "content": "The gate is shut.", "layer": "event"},
        {"title": "Gate", "content": "The gate creaks.", "layer": "event"},
    ]
    entries, _ = knowledge_extractor.plan_import(proposals)
    assert entries[0].title == "Gate"
    assert entries[1].title != entries[0].title


@pytest.mark.asyncio
async def test_extract_from_campaign_proposes_but_does_not_write(tmp_path):
    store = ScenarioStore(str(tmp_path / "scenarios.db"))
    scenario = store.create_scenario(title="\u96fe\u6e2f")
    game = _GameStore([_turn("The Old Mill is a ruined tower. You stand at the crossroads.", "req-1", 4)])
    result = await knowledge_extractor.extract_from_campaign(game, store, scenario.id, "camp-1")
    assert result["applied"] is False
    assert result["turns_read"] == 1
    assert len(result["entries"]) == 2
    assert result["created_ids"] == []
    assert store.list_knowledge(scenario.id) == []
    store.close()


@pytest.mark.asyncio
async def test_extract_from_campaign_applies_once(tmp_path):
    store = ScenarioStore(str(tmp_path / "scenarios.db"))
    scenario = store.create_scenario(title="\u96fe\u6e2f")
    game = _GameStore([_turn("The keeper watches the gate.", "req-1", 4)])
    first = await knowledge_extractor.extract_from_campaign(game, store, scenario.id, "camp-1", apply=True)
    assert len(first["created_ids"]) == 1
    stored = store.list_knowledge(scenario.id)
    assert [entry.content for entry in stored] == ["The keeper watches the gate."]
    second = await knowledge_extractor.extract_from_campaign(game, store, scenario.id, "camp-1", apply=True)
    assert second["created_ids"] == []
    assert len(store.list_knowledge(scenario.id)) == 1
    store.close()


@pytest.mark.asyncio
async def test_extract_from_campaign_reports_a_broken_model(tmp_path):
    class _Broken:
        async def complete(self, messages, max_tokens=0, temperature=0.0):
            raise RuntimeError("model down")

    store = ScenarioStore(str(tmp_path / "scenarios.db"))
    scenario = store.create_scenario(title="\u96fe\u6e2f")
    game = _GameStore([_turn("The keeper watches the gate.", "req-1", 4)])
    result = await knowledge_extractor.extract_from_campaign(
        game, store, scenario.id, "camp-1", router=_Broken()
    )
    assert len(result["entries"]) == 1
    assert result["warnings"] and "model down" in result["warnings"][0]
    store.close()


class _GameStore:
    def __init__(self, rows):
        self._rows = rows

    def list_events(self, campaign_id, limit=200, after_version=0):
        return list(self._rows)
