"""Phase 2: talking to an NPC is narrated from that NPC's own dossier.

These run on the deterministic template narrator, so the assertions are about
what the engine can state without a model — the guarantee the acceptance
scenarios lean on when the model is unavailable (scenario 6).
"""
from __future__ import annotations

import pytest

from app.models.game import (
    ActionRequest,
    EntityState,
    GameState,
    RuleOutcome,
    WorldDefinition,
)
from app.services.narrative_service import LLMNarrator, TemplateNarrator


def _world() -> WorldDefinition:
    return WorldDefinition.model_validate(
        {
            "world_id": "w1",
            "name": "Village",
            "scenario_id": "s1",
            "start_location_id": "square",
            "locations": [{"id": "square", "name": "Square", "description": "A stone square."}],
            "items": [{"id": "bread", "name": "Bread"}],
            "entities": [
                {
                    "id": "priest",
                    "name": "Old Priest",
                    "kind": "npc",
                    "location_id": "square",
                    "description": "A patient old man with tired eyes.",
                    "dialogue": ["The peace be with you."],
                    "attributes": {"personality": "calm; devout"},
                },
                {
                    "id": "merchant",
                    "name": "Merchant",
                    "kind": "npc",
                    "location_id": "square",
                    "description": "A sharp-eyed trader.",
                    "dialogue": ["Coin first."],
                },
            ],
        }
    )


def _state() -> GameState:
    state = GameState(campaign_id="c1", world_id="w1", location_id="square", turn=2)
    for entity_id in ("priest", "merchant"):
        state.entities[entity_id] = EntityState(
            entity_id=entity_id, location_id="square", hp=10, max_hp=10
        )
    state.entities["priest"].flags["mood"] = "wary"
    return state


def _talk(target: str) -> ActionRequest:
    request_id = f"r-{target}"
    from app.models.game import ActionRequest

    return ActionRequest(
        campaign_id="c1",
        request_id=request_id, action="talk", target_id=target, params={}, narrate=True
    )


def _ok() -> RuleOutcome:
    return RuleOutcome(ok=True, code="ok", message="You speak with them.")


class Memory:
    def __init__(self, windows=None):
        self.windows = windows or {}

    def build_npc_knowledge_window(self, campaign_id, npc_name):
        return self.windows.get(npc_name, "")


@pytest.mark.asyncio
async def test_template_narrator_speaks_as_the_addressed_npc():
    from app.engines.player_context import PlayerContextBuilder

    narrator = TemplateNarrator(PlayerContextBuilder())
    result = await narrator.narrate(
        _talk("priest"), _ok(), _state(), _world()
    )
    assert result.status == "ok"
    assert "patient old man" in result.text
    # The line is built from the addressed NPC's dossier only.
    assert "tired eyes" in result.text
    assert "trader" not in result.text


@pytest.mark.asyncio
async def test_template_narrator_without_builder_keeps_the_old_line():
    narrator = TemplateNarrator()
    result = await narrator.narrate(_talk("priest"), _ok(), _state(), _world())
    assert result.status == "ok"
    assert result.text == "You speak with them."


@pytest.mark.asyncio
async def test_each_npc_gets_its_own_witnessed_memory():
    from app.engines.player_context import PlayerContextBuilder

    memory = Memory({"Old Priest": "=== PRMNT_MEM (witnessed) ===\n- you confessed to the theft"})
    narrator = TemplateNarrator(PlayerContextBuilder(memory_provider=memory))
    priest = await narrator.narrate(_talk("priest"), _ok(), _state(), _world())
    merchant = await narrator.narrate(_talk("merchant"), _ok(), _state(), _world())
    assert "confessed to the theft" in priest.text
    assert "confessed to the theft" not in merchant.text


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_llm_narrator_puts_the_npc_layers_in_the_prompt():
    from app.engines.player_context import PlayerContextBuilder

    captured = {}

    class Router:
        config = None

        async def complete(self, messages, **kwargs):
            captured["messages"] = messages
            return "The priest nods slowly."

    narrator = LLMNarrator(
        Router(), context_builder=PlayerContextBuilder(), timeout=5.0
    )
    result = await narrator.narrate(_talk("priest"), _ok(), _state(), _world())
    assert result.status == "ok"
    prompt = captured["messages"][1]["content"]
    assert "Layered player context" in prompt
    assert "Old Priest" in prompt
    assert "belief" in prompt or "fact" in prompt
    kinds = result.diagnostics.get("context_kinds") or {}
    assert kinds.get("scene") == "fact"
    assert kinds.get("npc_identity") == "fact"


@pytest.mark.asyncio
async def test_non_talk_actions_do_not_pay_for_a_dossier():
    from app.engines.player_context import PlayerContextBuilder

    captured = {}

    class Router:
        config = None

        async def complete(self, messages, **kwargs):
            captured["messages"] = messages
            return "You look around."

    from app.models.game import ActionRequest

    narrator = LLMNarrator(Router(), context_builder=PlayerContextBuilder(), timeout=5.0)
    request = ActionRequest(
        campaign_id="c1", request_id="r-look", action="look", target_id="", params={}, narrate=True
    )
    result = await narrator.narrate(request, _ok(), _state(), _world())
    assert result.status == "ok"
    assert "Layered player context" not in captured["messages"][1]["content"]


@pytest.mark.asyncio
async def test_narrative_role_uses_its_own_router():
    from app.engines.agent_router import AgentRuntime

    seen = []

    import app.engines.agent_router as agent_module
    from app.engines.llm_router import LLMConfig, LLMProvider, LLMRouter

    # A role on another provider would build a real router; the point of the
    # test is which *route* is picked, so every route reports its model name.
    base = LLMRouter(LLMConfig(primary_provider=LLMProvider.DEEPSEEK, primary_model="base-model"))

    async def fake_complete(router, messages, **kwargs):
        seen.append(router.config.primary_model)
        return "A line."

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(agent_module, "_complete_with_timeout", fake_complete)
    try:
        runtime = AgentRuntime(base, environ={
            "AGENTS_NARRATIVE_PROVIDER": "deepseek",
            "AGENTS_NARRATIVE_MODEL": "narrative-model",
        })
        narrator = LLMNarrator(base, agents=runtime, timeout=5.0)
        await narrator.narrate(_talk("priest"), _ok(), _state(), _world())
    finally:
        monkeypatch.undo()
    assert seen == ["narrative-model"]
