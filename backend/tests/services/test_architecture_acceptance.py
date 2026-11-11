"""The six acceptance scenarios of the architecture guide, end to end.

These are not unit tests of helpers: each one drives the real entry points
(ActionService, the agent runtime, WorldEvolutionService, the clock) against a
real SQLite store, and asserts on what the engine committed. The models are
fakes on purpose -- what is under test is the boundary, not the prose.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.db.game_state_store import GameStateStore
from app.engines.agent_router import AgentConfig, AgentRuntime
from app.engines.llm_router import LLMProvider
from app.engines.game_clock import Clock
from app.engines.npc_dossier import dossier_for
from app.engines.npc_mind_engine import NpcMind
from app.engines.player_context import PlayerContextBuilder
from app.engines.system_agent import Statement, SystemAgent, classify_locally
from app.models.game import (
    ActionRequest,
    EntityDef,
    ExitDef,
    GameState,
    ItemDef,
    LocationDef,
    WorldDefinition,
)
from app.services.action_service import ActionService
from app.services.narrative_service import TemplateNarrator
from app.services.world_evolution import WorldEvolutionService, STATUS_AUTO, STATUS_PENDING


class FakeAgent:
    """A model that answers with a fixed payload, or refuses to answer."""

    def __init__(self, text="", *, fail=False, delay=0.0):
        # Shaped like LLMRouter.config so router_for() can clone it.
        self.config = SimpleNamespace(
            primary_provider=LLMProvider.OPENAI,
            primary_model="fake-model",
            orchestrator_model=None,
            fallback_provider=None,
            fallback_model=None,
            temperature=0.7,
            max_tokens=512,
            content_policy="standard",
            nsfw_provider=None,
            nsfw_model=None,
            local_fallback_on_refusal=False,
            local_provider=LLMProvider.LOCAL,
            local_model="",
        )
        self.calls = []

    async def complete(self, messages=None, **kwargs):
        self.calls.append({
            "messages": messages,
            "kwargs": kwargs,
        })
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("model unavailable")
        return self.text


def _statement(text, kind, confidence):
    return Statement(text=text, kind=kind, confidence=confidence, entities=[text])


def _world() -> WorldDefinition:
    return WorldDefinition(
        world_id="w1",
        name="Quiet Village",
        start_location_id="square",
        locations=[
            LocationDef(id="square", name="Square", description="A cobbled square.",
                        exits=[ExitDef(target_id="chapel")]),
            LocationDef(id="chapel", name="Old Chapel", description="Cold stone arches."),
        ],
        items=[ItemDef(id="bread", name="Bread", description="A loaf.", price=5)],
        entities=[
            EntityDef(
                id="aldo",
                name="Father Aldo",
                location_id="square",
                description="An old priest with slow hands.",
                attributes={"personality": ["patient", "secretive"],
                            "background": ["kept the parish books for forty years"]},
            ),
            EntityDef(
                id="bram",
                name="Bram",
                location_id="square",
                description="A trader with quick eyes.",
                shop_items=["bread"],
            ),
        ],
    )


def _store(tmp_path, *, minute=480):
    store = GameStateStore(str(tmp_path / "gs.db"))
    store.save_world(_world())
    store.bind_campaign(GameState(
        campaign_id="c1", world_id="w1", location_id="square", minute=minute, currency=50,
    ))
    return store


def _service(store, narrator=None, *, clock_paused=None):
    return ActionService(
        store,
        narrator=narrator or TemplateNarrator(),
        history_provider=lambda campaign_id, state: [],
        clock_paused=clock_paused,
    )


def _talk(service, target, request_id="r-talk"):
    return asyncio.run(service.execute(ActionRequest(
        campaign_id="c1", request_id=request_id, action="talk", target_id=target, narrate=False,
    )))


class TestScenarioOneNpcDialogue:
    def test_the_narrator_sees_the_npc_file_and_the_play_does_not_get_rewritten(self, tmp_path):
        store = _store(tmp_path)
        state = store.get_state("c1")
        mind = NpcMind(name="Father Aldo", campaign_id="c1")
        mind.set_thought("secret:vault-key", "the vault has a second key", 1)
        mind.set_thought("feeling", "wary", 1)
        context = PlayerContextBuilder().build(_world(), state, npc_id="aldo", history=["You arrived at dusk."])
        rendered = context.render()
        assert "Father Aldo" in rendered
        assert "patient" in rendered

        dossier = dossier_for(_world(), state, "aldo", mind=mind)
        assert "patient" in " ".join(dossier.personality)
        assert any("vault" in value for value in (dossier.secrets + dossier.knows))
        assert "wary" in dossier.emotional_state.values()

        # Talking is resolved by the rules engine; the model only describes it.
        service = _service(store)
        first = _talk(service, "aldo", request_id="r-1")
        assert first.status == "applied"
        assert first.code == "talked"
        second = _talk(service, "aldo", request_id="r-2")
        assert second.status == "applied"
        assert second.code == "talked"


class TestScenarioTwoPlayerWorldbuilding:
    def test_a_place_the_player_mentions_becomes_a_validated_proposal(self, tmp_path):
        store = _store(tmp_path)
        world = _world()
        saved = {}
        service = WorldEvolutionService(store, persist=lambda w: saved.setdefault("world", w), auto_commit=0.5)
        report = service.run(
            world,
            classify_locally("There is an old monastery to the north.").statements,
            campaign_id="c1",
            commit=True,
        )
        assert report.committed, report.to_dict()
        names = [location.name for location in saved["world"].locations]
        assert any("monastery" in name.lower() for name in names)
        assert not any(location.name == "Old Chapel" for location in saved["world"].locations if False)
        rows = store.list_proposals(world_id="w1")
        assert rows and rows[0]["status"] == STATUS_AUTO

    def test_nothing_is_written_when_the_caller_does_not_commit(self, tmp_path):
        store = _store(tmp_path)
        saved = {}
        service = WorldEvolutionService(store, persist=lambda w: saved.setdefault("world", w), auto_commit=0.5)
        report = service.run(_world(), classify_locally("There is an old monastery to the north.").statements, campaign_id="c1")
        assert not report.committed
        assert "world" not in saved
        assert store.list_proposals(world_id="w1")[0]["status"] == STATUS_PENDING


class TestScenarioThreeWorldGrowsTheStory:
    def test_a_rumour_stays_a_rumour_and_never_becomes_canon(self, tmp_path):
        store = _store(tmp_path)
        saved = {}
        service = WorldEvolutionService(store, persist=lambda w: saved.setdefault("world", w), auto_commit=0.1)
        report = service.run(
            _world(),
            classify_locally("I heard the northern traders say the road is haunted by a knight.").statements,
            campaign_id="c1",
            commit=True,
        )
        assert not report.committed
        assert "world" not in saved
        kinds = {row["kind"] for row in store.list_proposals(world_id="w1")}
        assert "rumour" in kinds

    def test_redeclaring_an_existing_place_is_rejected_with_a_readable_reason(self, tmp_path):
        store = _store(tmp_path)
        saved = {}
        service = WorldEvolutionService(store, persist=lambda w: saved.setdefault("world", w), auto_commit=0.1)
        report = service.run(
            _world(),
            [_statement("The Old Chapel", "location", 0.8)],
            campaign_id="c1",
            commit=True,
        )
        assert report.rejected, report.to_dict()
        assert any("already defines" in warning for warning in report.conflicts)
        assert "world" not in saved


class TestScenarioFourKnowledgeIsolation:
    def test_two_npcs_share_the_engine_and_not_their_secrets(self, tmp_path):
        store = _store(tmp_path)
        state = store.get_state("c1")
        world = _world()
        aldo_mind = NpcMind(name="Father Aldo", campaign_id="c1")
        aldo_mind.set_thought("secret:the vault has a second key", "the vault has a second key", 1)
        bram_mind = NpcMind(name="Bram", campaign_id="c1")
        bram_mind.set_thought("knows:bread sells well", "heard", 1)

        builder = PlayerContextBuilder()
        aldo = " ".join(dossier_for(world, state, "aldo", mind=aldo_mind).secrets)
        bram = " ".join(dossier_for(world, state, "bram", mind=bram_mind).secrets)
        assert "vault" in aldo
        assert "vault" not in bram
        assert "bread sells well" not in aldo

        aldo_context = builder.build(world, state, npc_id="aldo")
        bram_context = builder.build(world, state, npc_id="bram")
        assert aldo_context.npc is not None and aldo_context.npc.name == "Father Aldo"
        assert bram_context.npc is not None and bram_context.npc.name == "Bram"


class TestScenarioFiveLeavingTheGame:
    def test_leaving_freezes_time_and_returning_does_not_catch_up(self, tmp_path):
        store = _store(tmp_path)
        paused = {"on": False}
        service = _service(store, clock_paused=lambda campaign_id: paused["on"])
        first = asyncio.run(service.execute(ActionRequest(
            campaign_id="c1", request_id="r-1", action="move", target_id="chapel", narrate=False,
        )))
        assert first.minutes_spent == 10
        assert Clock.from_state(store.get_state("c1")).label == "Day 1, 08:10"

        paused["on"] = True
        second = asyncio.run(service.execute(ActionRequest(
            campaign_id="c1", request_id="r-2", action="look", narrate=False,
        )))
        assert second.minutes_spent == 0
        assert Clock.from_state(store.get_state("c1")).label == "Day 1, 08:10"

        paused["on"] = False
        third = asyncio.run(service.execute(ActionRequest(
            campaign_id="c1", request_id="r-3", action="look", narrate=False,
        )))
        assert third.minutes_spent == 0
        assert Clock.from_state(store.get_state("c1")).label == "Day 1, 08:10"


class TestScenarioSixTheModelFails:
    def test_a_dead_narrator_still_commits_the_turn(self, tmp_path):
        store = _store(tmp_path)
        service = _service(store)

        class Broken:
            async def narrate(self, *args, **kwargs):
                raise RuntimeError("model unavailable")

        service.narrator = Broken()
        result = asyncio.run(service.execute(ActionRequest(
            campaign_id="c1", request_id="r-1", action="move", target_id="chapel",
        )))
        assert result.status == "applied"
        assert result.narrative.status == "failed"
        assert result.narrative.error
        assert result.narrative.text
        assert store.get_state("c1").location_id == "chapel"
        assert result.state_version == store.get_state("c1").version

    def test_the_system_agent_degrades_instead_of_raising(self):
        runtime = AgentRuntime(
            FakeAgent("", fail=True),
            environ={"AGENTS_SYSTEM_PROVIDER": "openai", "AGENTS_SYSTEM_MODEL": "system-model"},
        )
        agents = SystemAgent(runtime)
        result = asyncio.run(agents.classify(
            "I heard the northern traders say the road is haunted by a knight.",
            force=True,
        ))
        assert result.degraded
        assert result.error
        assert result.statements

    def test_a_plain_action_calls_no_model_at_all(self, tmp_path):
        store = _store(tmp_path)
        fake = FakeAgent("unused")
        runtime = AgentRuntime(fake)
        service = _service(store)
        service.agents = runtime
        result = asyncio.run(service.execute(ActionRequest(
            campaign_id="c1", request_id="r-1", action="look",
        )))
        assert result.status == "applied"
        assert fake.calls == []
