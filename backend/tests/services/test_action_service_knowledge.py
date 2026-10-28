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
