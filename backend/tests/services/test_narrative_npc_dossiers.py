"""Phase 2: one shared narrative model, separate NPC files.

The play loop streams narration through ``_handle_narrative`` rather than
through ActionService, so the NPC dossier has to reach *that* prompt too.
These tests drive the real method with a stub narrator and assert on the
system prompt the model would have received.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.game import EntityDef


class RecordingNarrator:
    """Captures the prompts and streams one canned line."""

    def __init__(self):
        self.calls = []

    def build_system_prompt(self, **kwargs):
        return "BASE " + (kwargs.get("narrator_hints") or "")

    async def stream_narrative(self, prompt, system_prompt, history, **kwargs):
        self.calls.append(system_prompt)
        yield "The innkeeper nods."


class RecordingMinds:
    def __init__(self, minds):
        self._minds = minds

    def get_all_minds(self, campaign_id):
        return list(self._minds.values())

    def get_mind(self, campaign_id, name):
        return self._minds.get(name.lower())


def _mind(name, thoughts):
    return SimpleNamespace(
        name=name,
        thoughts={key: SimpleNamespace(value=value) for key, value in thoughts.items()},
    )


def _entity(entity_id, name, **attributes):
    return EntityDef(
        id=entity_id,
        name=name,
        description=f"{name} watches the room.",
        location_id="inn",
        kind="npc",
        attributes=attributes,
    )


@pytest.mark.asyncio
async def test_the_narrator_receives_each_npcs_stable_identity():
    from app.services.game_session import GameSession

    narrator = RecordingNarrator()
    session = object.__new__(GameSession)
    session.campaign_id = "c1"
    session._agents = object()  # role routing is on
    session._narrator = narrator
    session._scenario = SimpleNamespace(
        entities={
            "innkeeper": _entity("innkeeper", "Mira", personality="warm, guarded"),
        }
    )
    session._npc_minds = RecordingMinds({})

    block = session._npc_dossier_block({"mira"})

    assert "NPC CHARACTER FILES" in block
    assert "Mira" in block
    assert "warm, guarded" in block


@pytest.mark.asyncio
async def test_two_npcs_do_not_share_a_file_or_a_secret():
    from app.services.game_session import GameSession

    session = object.__new__(GameSession)
    session.campaign_id = "c1"
    session._agents = object()
    session._scenario = SimpleNamespace(
        entities={
            "priest": _entity("priest", "Father Aldo"),
            "merchant": _entity("merchant", "Bram"),
        }
    )
    session._npc_minds = RecordingMinds({
        "father aldo": _mind("Father Aldo", {"secret_plan": "he buried the ledger"}),
        "bram": _mind("Bram", {"feeling": "impatient"}),
    })

    block = session._npc_dossier_block({"father aldo", "bram"})

    # Sorted by name, so Bram's file is written before the priest's.
    merchant_section = block.split("--- Bram ---")[1].split("--- Father Aldo ---")[0]
    priest_section = block.split("--- Father Aldo ---")[1]
    assert "buried the ledger" in priest_section
    assert "buried the ledger" not in merchant_section
    assert "impatient" in merchant_section


@pytest.mark.asyncio
async def test_no_role_routing_means_no_extra_prompt_text():
    from app.services.game_session import GameSession

    session = object.__new__(GameSession)
    session.campaign_id = "c1"
    session._agents = None
    session._scenario = SimpleNamespace(entities={"a": _entity("a", "Ann")})
    session._npc_minds = RecordingMinds({})

    assert session._npc_dossier_block({"ann"}) == ""


@pytest.mark.asyncio
async def test_an_unknown_name_is_skipped_rather_than_invented():
    from app.services.game_session import GameSession

    session = object.__new__(GameSession)
    session.campaign_id = "c1"
    session._agents = object()
    session._scenario = SimpleNamespace(entities={})
    session._npc_minds = RecordingMinds({})

    assert session._npc_dossier_block({"ghost"}) == ""
