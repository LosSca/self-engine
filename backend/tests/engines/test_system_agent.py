"""Phase 1/2: the System Agent classifies and proposes, and never writes state."""
from __future__ import annotations

import json

import pytest

from app.engines.agent_router import AgentRuntime
from app.engines.system_agent import (
    CANON_KINDS,
    INFO_KINDS,
    OP_KINDS,
    UNVERIFIED_KINDS,
    Classification,
    Statement,
    SystemAgent,
    classify_locally,
    classify_text,
    initial_confidence,
    is_canon_candidate,
    needs_agent,
    parse_agent_payload,
)
from app.engines.llm_router import LLMConfig, LLMRouter


class FakeRuntime:
    """Stands in for AgentRuntime: records what it was asked, returns canned JSON."""

    def __init__(self, answer="", *, ok=True, enabled=True):
        self.answer = answer
        self.ok = ok
        self.enabled = enabled
        self.calls = []

    def config(self, role):
        return type("C", (), {"enabled": self.enabled, "role": role})()

    async def ask(self, role, messages, *, task="", **_kw):
        self.calls.append({"role": role, "task": task, "messages": messages})
        return type(
            "R",
            (),
            {
                "ok": self.ok,
                "text": self.answer,
                "error": "" if self.ok else "RuntimeError: boom",
                "degraded": not self.ok,
                "role": role,
            },
        )()


def _payload(statements):
    return json.dumps({"statements": statements})


# ------------------------------------------------------------------- rules
def test_a_rumour_is_not_a_fact():
    assert classify_text("They say the northern road is closed.") == "rumour"
    assert classify_text("听说北方商路封了。") == "rumour"


def test_a_belief_belongs_to_one_character():
    assert classify_text("The priest believes the abbot is alive.") == "belief"


def test_a_guess_is_a_hypothesis():
    assert classify_text("The cellar door is perhaps sealed.") == "hypothesis"


def test_player_backstory_is_its_own_kind():
    assert classify_text("When I was a child I lived in a monastery.") == "player_backstory"


def test_a_place_and_an_object_are_extracted():
    assert classify_text("The old monastery stands on the hill.") == "location"
    assert classify_text("A brass key lies on the table.") == "item"


def test_questions_are_never_classified():
    assert classify_text("Where is the key?") is None
    assert classify_text("钥匙在哪里？") is None


def test_unverified_kinds_score_lower_than_canon():
    assert initial_confidence("rumour") < initial_confidence("canonical_fact")
    assert initial_confidence("hypothesis") < initial_confidence("rumour")
    for kind in INFO_KINDS:
        score = initial_confidence(kind)
        assert 0.0 <= score <= 1.0


def test_local_classification_is_deterministic_and_needs_no_model():
    first = classify_locally("They say the road is closed.")
    second = classify_locally("They say the road is closed.")
    assert [s.to_dict() for s in first.statements] == [s.to_dict() for s in second.statements]
    assert first.consulted_agent is False


def test_only_uncertain_text_asks_the_model():
    certain = classify_locally("The key lies on the table.")
    assert needs_agent(certain.statements) is False
    uncertain = classify_locally("They say the northern merchant road is closed by the duke.")
    assert needs_agent(uncertain.statements) is True


# --------------------------------------------------------------- parsing
def test_unknown_kinds_are_dropped():
    payload = {"statements": [
        {"text": "The road is closed.", "kind": "prophecy", "confidence": 0.9},
        {"text": "The road is open.", "kind": "canonical_fact", "confidence": 0.9},
    ]}
    parsed = parse_agent_payload(payload)
    assert [s.kind for s in parsed] == ["canonical_fact"]


def test_confidence_is_clamped_and_defaulted():
    payload = {"statements": [
        {"text": "a", "kind": "rumour", "confidence": 7},
        {"text": "b", "kind": "rumour", "confidence": "not a number"},
    ]}
    parsed = parse_agent_payload(payload)
    assert parsed[0].confidence == 1.0
    assert parsed[1].confidence == initial_confidence("rumour")


def test_garbage_payload_parses_to_nothing():
    assert parse_agent_payload(None) == []
    assert parse_agent_payload({"statements": "nope"}) == []
    assert parse_agent_payload({"statements": [1, 2, None]}) == []


def test_only_confident_canon_without_conflict_may_be_written():
    assert is_canon_candidate(Statement(text="x", kind="canonical_fact", confidence=0.9))
    assert not is_canon_candidate(Statement(text="x", kind="canonical_fact", confidence=0.2))
    assert not is_canon_candidate(
        Statement(text="x", kind="canonical_fact", confidence=0.9, conflict=True)
    )
    for kind in UNVERIFIED_KINDS:
        assert not is_canon_candidate(Statement(text="x", kind=kind, confidence=1.0))
    assert CANON_KINDS == ("canonical_fact",)


# ----------------------------------------------------------------- agent
@pytest.mark.asyncio
async def test_a_confident_message_never_calls_the_model():
    runtime = FakeRuntime(_payload([]))
    agent = SystemAgent(runtime)
    result = await agent.classify("The key lies on the table.")
    assert runtime.calls == []
    assert result.consulted_agent is False
    assert result.statements[0].kind == "item"


@pytest.mark.asyncio
async def test_an_uncertain_message_asks_the_system_role_and_merges():
    runtime = FakeRuntime(_payload([
        {
            "text": "They say the northern merchant road is closed by the duke.",
            "kind": "canonical_fact",
            "confidence": 0.9,
        }
    ]))
    agent = SystemAgent(runtime)
    result = await agent.classify("They say the northern merchant road is closed by the duke.")
    assert runtime.calls and runtime.calls[0]["role"] == "system"
    assert [s.origin for s in result.statements] == ["agent"]
    assert result.statements[0].kind == "canonical_fact"


@pytest.mark.asyncio
async def test_a_failed_agent_falls_back_to_rules_without_raising():
    agent = SystemAgent(FakeRuntime("", ok=False))
    result = await agent.classify(
        "They say the northern merchant road is closed by the duke."
    )
    assert result.degraded is True
    assert result.statements and result.statements[0].kind == "rumour"
    assert result.warnings


@pytest.mark.asyncio
async def test_a_disabled_agent_is_not_consulted():
    runtime = FakeRuntime(_payload([]), enabled=False)
    agent = SystemAgent(runtime)
    result = await agent.classify(
        "They say the northern merchant road is closed by the duke."
    )
    assert runtime.calls == []
    assert result.degraded is True


@pytest.mark.asyncio
async def test_a_non_json_answer_falls_back_to_rules():
    agent = SystemAgent(FakeRuntime("I think it is a rumour, definitely."))
    result = await agent.classify(
        "They say the northern merchant road is closed by the duke."
    )
    assert result.degraded is True
    assert result.statements[0].origin == "rules"


@pytest.mark.asyncio
async def test_conflicts_are_reported_but_change_nothing():
    runtime = FakeRuntime(_payload([{
        "text": "They say the northern merchant road is closed by the duke.",
        "kind": "canonical_fact",
        "confidence": 0.9,
        "conflict": True,
        "conflicts_with": "The northern road is open.",
    }]))
    agent = SystemAgent(runtime)
    result = await agent.classify(
        "They say the northern merchant road is closed by the duke."
    )
    assert result.conflicts
    assert "northern road is open" in result.conflicts[0]
    assert not is_canon_candidate(result.statements[0])


@pytest.mark.asyncio
async def test_ops_are_filtered_to_known_kinds():
    runtime = FakeRuntime(json.dumps({"ops": [
        {"kind": "add_location", "data": {"id": "monastery"}},
        {"kind": "delete_everything", "data": {}},
        {"kind": "add_item", "data": "not a dict"},
    ]}))
    agent = SystemAgent(runtime)
    ops = await agent.review_ops("I grew up in a monastery.")
    assert ops == [{"kind": "add_location", "data": {"id": "monastery"}}]
    assert "add_location" in OP_KINDS


@pytest.mark.asyncio
async def test_without_a_runtime_nothing_is_asked():
    agent = SystemAgent(None)
    result = await agent.classify("They say the road is closed, people keep whispering about it.")
    assert result.degraded is True
    assert await agent.review_ops("anything") == []


@pytest.mark.asyncio
async def test_the_agent_runtime_is_wired_through_env(monkeypatch):
    """End to end: a role config routes the call, and failures degrade."""
    router = LLMRouter(LLMConfig())
    seen = []

    async def fake_complete(router_, messages, **kwargs):
        seen.append(router_.config.primary_model)
        raise RuntimeError("upstream down")

    import app.engines.agent_router as module

    original = module._complete_with_timeout
    module._complete_with_timeout = fake_complete
    try:
        runtime = AgentRuntime(router, environ={
            "AGENTS_SYSTEM_PROVIDER": "openai",
            "AGENTS_SYSTEM_MODEL": "system-model",
            "AGENTS_NARRATIVE_PROVIDER": "anthropic",
            "AGENTS_NARRATIVE_MODEL": "narrative-model",
        })
        result = await SystemAgent(runtime).classify(
            "They say the northern merchant road is closed by the duke."
        )
    finally:
        module._complete_with_timeout = original
    assert seen == ["system-model", "narrative-model"]
    assert result.degraded is True
    assert result.statements[0].origin == "rules"
