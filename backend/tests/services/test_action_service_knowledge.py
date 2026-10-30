"""The action pipeline with authored knowledge, play modes and model presets."""
import pytest

from app.db.game_state_store import GameStateStore
from app.db.scenario_store import ScenarioStore
from app.engines.story_engine import storyline_status
from app.models.game import ActionRequest, GameState, NarrativeResult
from app.models.knowledge import (
    KnowledgeEntryCreate,
    ModelPresetCreate,
    PlayConfig,
    ScenarioPlayContext,
    Storyline,
    StoryNode,
)
from app.services.action_service import ActionService
from app.services.world_service import bind_world, save_world
from tests.support import village_world


class RecordingNarrator:
    def __init__(self):
        self.calls = []

    async def narrate(
        self, request, outcome, state, world, executions=(), quest_updates=(),
        knowledge=None, options=None,
    ):
        self.calls.append(
            {"knowledge": list(knowledge or []), "options": options, "state": state}
        )
        return NarrativeResult(status="ok", text="叙述已生成", model="stub")


class LegacyNarrator:
    """A narrator written before authored context existed."""

    async def narrate(self, request, outcome, state, world, executions=(), quest_updates=()):
        return NarrativeResult(status="ok", text="旧叙事器仍然可用")


@pytest.fixture
def stores(tmp_path):
    scenario_store = ScenarioStore(str(tmp_path / "scenarios.db"))
    game_store = GameStateStore(str(tmp_path / "game_state.db"))
    yield scenario_store, game_store
    game_store.close()
    scenario_store.close()


@pytest.fixture
def scenario(stores):
    return stores[0].create_scenario(title="雾港", language="zh")


@pytest.fixture
def world(stores, scenario):
    scenario_store, game_store = stores
    world = village_world().model_copy(update={"scenario_id": scenario.id})
    save_world(game_store, world)
    bind_world(game_store, "camp-1", world.world_id, reset=True)
    return world


def _provider(scenario_store):
    def provider(scenario_id):
        config = scenario_store.get_play_config(scenario_id)
        preset = (
            scenario_store.get_model_preset(config.model_preset_id)
            if config.model_preset_id
            else None
        )
        return ScenarioPlayContext(
            scenario_id=scenario_id,
            config=config,
            entries=scenario_store.list_knowledge(scenario_id),
            preset=preset,
        )

    return provider


def _request(request_id, action, target=None, narrate=False):
    return ActionRequest(
        campaign_id="camp-1",
        request_id=request_id,
        action=action,
        target_id=target,
        narrate=narrate,
    )


def _storyline():
    return Storyline(
        name="仓库线",
        nodes=[
            StoryNode(id="arrive", title="抵达村庄"),
            StoryNode(
                id="open",
                title="打开仓库",
                prereq=["arrive"],
                conditions=[{"kind": "door_open", "target": "warehouse_door"}],
            ),
        ],
    )


def _set_storyline(scenario_store, scenario_id, nodes=None, mode="storyline", **kwargs):
    return scenario_store.set_play_config(
        scenario_id,
        PlayConfig(
            scenario_id=scenario_id,
            mode=mode,
            storyline=Storyline(name="仓库线", nodes=nodes if nodes is not None else _storyline().nodes),
            **kwargs,
        ),
    )


# -------------------------------------------------------------- story mode
async def test_storyline_mode_enters_nodes_and_persists_them(stores, scenario, world):
    scenario_store, game_store = stores
    _set_storyline(scenario_store, scenario.id)
    service = ActionService(
        game_store, context_provider=_provider(scenario_store)
    )

    taken = await service.execute(_request("req-take", "take", "iron_key"))
    assert taken.status == "applied"
    assert taken.story_nodes_entered == ["arrive"]
    assert taken.state.story_nodes == ["arrive"]

    opened = await service.execute(_request("req-open", "open", "warehouse_door"))
    assert opened.story_nodes_entered == ["open"]
    assert opened.state.story_nodes == ["arrive", "open"]

    stored = game_store.get_state("camp-1")
    assert stored.story_nodes == ["arrive", "open"]

    events = game_store.list_events("camp-1")
    story_events = [event for event in events if event["event_type"] == "story.entered"]
    assert [event["payload"]["node_id"] for event in story_events] == ["arrive", "open"]


async def test_free_mode_never_enters_story_nodes(stores, scenario, world):
    scenario_store, game_store = stores
    _set_storyline(scenario_store, scenario.id, mode="free")
    service = ActionService(game_store, context_provider=_provider(scenario_store))

    taken = await service.execute(_request("req-take", "take", "iron_key"))
    assert taken.status == "applied"
    assert taken.state.story_nodes == []
    assert taken.story_nodes_entered == []


async def test_storyline_config_with_broken_conditions_does_not_fail_the_action(
    stores, scenario, world
):
    scenario_store, game_store = stores
    _set_storyline(scenario_store, scenario.id)
    broken = ScenarioPlayContext(
        scenario_id=scenario.id,
        config=PlayConfig(
            scenario_id=scenario.id,
            mode="storyline",
            storyline=Storyline(name="坏线", nodes=[StoryNode(id="a", title="一")]),
        ),
        entries=[],
    )
    service = ActionService(game_store, context_provider=lambda _sid: broken)

    taken = await service.execute(_request("req-take", "take", "iron_key"))
    assert taken.status == "applied"
    assert taken.state_version == 1


# ------------------------------------------------------------ knowledge flow
@pytest.fixture
def multi_node_knowledge(stores, scenario, world):
    scenario_store, _game_store = stores
    entries = {
        name: scenario_store.add_knowledge(
            scenario.id,
            KnowledgeEntryCreate(
                layer="global" if name == "global" else "character",
                title=name, content=f"Knowledge for {name}", sort_order=index,
            ),
        ).id
        for index, name in enumerate(["global", "main", "branch", "side", "future"])
    }
    preset = scenario_store.add_model_preset(
        ModelPresetCreate(name="Story test", temperature=0.3, max_tokens=240)
    )
    _set_storyline(scenario_store, scenario.id, model_preset_id=preset.id, nodes=[
        StoryNode(id="arrive", title="Main", knowledge_ids=[entries["main"]]),
        StoryNode(id="branch", title="Branch", kind="branch", prereq=["arrive"],
                  knowledge_ids=[entries["branch"]]),
        StoryNode(id="side", title="Side", kind="side",
                  conditions=[{"kind": "at_location", "target": "village_square"}],
                  knowledge_ids=[entries["side"]]),
        StoryNode(id="open", title="Future", prereq=["arrive"],
                  conditions=[{"kind": "door_open", "target": "warehouse_door"}],
                  knowledge_ids=[entries["future"]]),
    ])
    return entries


@pytest.mark.parametrize("mode", ["storyline", "free"])
async def test_accepted_and_refused_narration_share_entered_node_context(
    stores, scenario, world, multi_node_knowledge, mode,
):
    scenario_store, game_store = stores
    history = {"id": "history:camp-1", "layer": "history", "title": "Recent turns",
               "content": "player: look around"}
    narrator = RecordingNarrator()
    service = ActionService(
        game_store, narrator=narrator, context_provider=_provider(scenario_store),
        history_provider=lambda campaign_id, state: [history],
    )
    initial = await service.execute(_request("enter", "look"))
    assert initial.story_nodes_entered == ["arrive", "branch", "side"]
    config = scenario_store.get_play_config(scenario.id)
    scenario_store.set_play_config(scenario.id, config.model_copy(update={"mode": mode}))

    accepted = await service.execute(_request("look", "look", narrate=True))
    before_refusal = game_store.get_state("camp-1").model_dump()
    refused = await service.execute(_request("refuse", "open", "warehouse_door", narrate=True))
    assert accepted.status == "applied"
    assert refused.status == "rejected"
    assert refused.code == "door_locked"
    assert accepted.narrative.status == refused.narrative.status == "ok"
    assert len(narrator.calls) == 2
    expected_names = (
        ["main", "branch", "side", "global", "future"] if mode == "storyline"
        else ["global", "main", "branch", "side", "future"]
    )
    expected_ids = [multi_node_knowledge[name] for name in expected_names] + [history["id"]]
    assert accepted.context_entries == refused.context_entries == expected_ids
    assert narrator.calls[0]["knowledge"] == narrator.calls[1]["knowledge"]
    assert [b["kind"] for b in narrator.calls[0]["knowledge"]] == ["canon"] * 5 + ["history"]
    assert narrator.calls[0]["knowledge"][-1] == dict(history, kind="history")
    assert narrator.calls[0]["options"] == narrator.calls[1]["options"] == {
        "temperature": 0.3, "max_tokens": 240,
    }
    assert accepted.story_nodes_entered == refused.story_nodes_entered == []
    assert refused.state.story_nodes == ["arrive", "branch", "side"]
    assert game_store.get_state("camp-1").model_dump() == before_refusal
    story_events = [e for e in game_store.list_events("camp-1") if e["event_type"] == "story.entered"]
    assert [e["payload"]["node_id"] for e in story_events] == ["arrive", "branch", "side"]


async def test_refusal_does_not_enter_or_boost_merely_eligible_nodes(
    stores, scenario, world, multi_node_knowledge,
):
    scenario_store, game_store = stores
    narrator = RecordingNarrator()
    service = ActionService(
        game_store, narrator=narrator, context_provider=_provider(scenario_store),
    )
    before = game_store.get_state("camp-1").model_dump()
    refused = await service.execute(_request("refuse", "open", "warehouse_door", narrate=True))
    assert refused.status == "rejected"
    assert refused.narrative.status == "ok"
    assert refused.context_entries == [
        multi_node_knowledge[name] for name in ["global", "main", "branch", "side", "future"]
    ]
    assert refused.state.story_nodes == []
    assert game_store.get_state("camp-1").model_dump() == before


@pytest.mark.parametrize("with_state", [False, True])
def test_knowledge_keeps_legacy_active_node_fallback(
    stores, scenario, multi_node_knowledge, with_state,
):
    scenario_store, game_store = stores
    service = ActionService(game_store)
    state = game_store.get_state("camp-1") if with_state else None
    blocks, entries, options = service._knowledge(_provider(scenario_store)(scenario.id), "arrive", state)
    assert entries == [
        multi_node_knowledge[name] for name in ["main", "global", "branch", "side", "future"]
    ]
    assert all(block["kind"] == "canon" for block in blocks)
    assert options == {"temperature": 0.3, "max_tokens": 240}


async def test_knowledge_blocks_reach_the_narrator(stores, scenario, world):
    scenario_store, game_store = stores
    scenario_store.add_knowledge(
        scenario.id,
        KnowledgeEntryCreate(layer="global", title="世界总览", content="雾港群岛", depth=1),
    )
    scenario_store.add_knowledge(
        scenario.id,
        KnowledgeEntryCreate(layer="story", title="故事线专属", content="隐藏", mode_scope="storyline"),
    )
    narrator = RecordingNarrator()
    service = ActionService(
        game_store,
        narrator=narrator,
        context_provider=_provider(scenario_store),
    )

    taken = await service.execute(_request("req-take", "take", "iron_key", narrate=True))
    assert taken.status == "applied"
    assert len(narrator.calls) == 1
    titles = [block["title"] for block in narrator.calls[0]["knowledge"]]
    assert titles == ["世界总览"]  # free mode drops storyline-only knowledge
    assert len(taken.context_entries) == 1
    assert taken.narrative.status == "ok"


async def test_narration_gets_preset_sampling_options(stores, scenario, world):
    scenario_store, game_store = stores
    preset = scenario_store.add_model_preset(
        ModelPresetCreate(name="紧张叙事", role="narrator", temperature=0.3, max_tokens=240)
    )
    _set_storyline(scenario_store, scenario.id, mode="free", model_preset_id=preset.id)
    narrator = RecordingNarrator()
    service = ActionService(
        game_store, narrator=narrator, context_provider=_provider(scenario_store)
    )

    await service.execute(_request("req-take", "take", "iron_key", narrate=True))
    assert narrator.calls[0]["options"] == {"temperature": 0.3, "max_tokens": 240}


async def test_context_provider_failure_degrades_instead_of_failing(stores, scenario, world):
    _scenario_store, game_store = stores

    def broken_provider(scenario_id):
        raise RuntimeError("scenario database is not reachable")

    service = ActionService(game_store, context_provider=broken_provider)
    taken = await service.execute(_request("req-take", "take", "iron_key", narrate=True))
    assert taken.status == "applied"
    assert taken.context_entries == []
    assert taken.narrative.status == "ok"


async def test_legacy_narrator_without_context_kwargs_still_runs(stores, scenario, world):
    scenario_store, game_store = stores
    scenario_store.add_knowledge(
        scenario.id, KnowledgeEntryCreate(layer="global", title="世界", content="雾")
    )
    service = ActionService(
        game_store,
        narrator=LegacyNarrator(),
        context_provider=_provider(scenario_store),
    )
    taken = await service.execute(_request("req-take", "take", "iron_key", narrate=True))
    assert taken.status == "applied"
    assert taken.narrative.text == "旧叙事器仍然可用"


async def test_replay_does_not_recompute_story_progress(stores, scenario, world):
    scenario_store, game_store = stores
    _set_storyline(scenario_store, scenario.id)
    service = ActionService(game_store, context_provider=_provider(scenario_store))

    await service.execute(_request("req-take", "take", "iron_key"))
    replay = await service.execute(_request("req-take", "take", "iron_key"))
    assert replay.status == "duplicate"
    assert replay.state.story_nodes == ["arrive"]
    assert game_store.get_state("camp-1").version == 1
