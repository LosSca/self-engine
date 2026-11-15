"""A character can be told something that is not true, and keeps it.

The P3 layer could already *derive* a perspective from claims, but nothing
ever wrote a claim, so the interesting case - the duke is certain Eileen is
dead and he is wrong - could only be built by hand in a test. These tests pin
the read path that makes it real: a belief lives in the NPC's own mind as a
thought named "belief:<predicate>:<subject>", memory reads it back, and the
world fact it contradicts stays visible *to the engine* while the prompt only
shows the character their own wrong version.
"""
from __future__ import annotations

import pytest

from app.engines.memory_engine import MemoryEngine
from app.services.memory_perspective import (
    BELIEVES,
    KNOWS,
    belief_key,
    build_perspective,
    facts_for_prompt,
    remember_belief,
)


class _Thought:
    def __init__(self, value):
        self.value = value


class _Mind:
    def __init__(self, thoughts=None):
        self.thoughts = dict(thoughts or {})

    def set_thought(self, key, value, current_turn=0, decay_after_turns=None):
        self.thoughts[key] = _Thought(value)
        self.thoughts[key].decay_after_turns = decay_after_turns


class _Minds:
    def __init__(self, minds=None):
        self._minds = dict(minds or {})

    def get_mind(self, campaign_id, npc_name):
        return self._minds.get(npc_name)


class _Fact:
    def __init__(self, subject_id="Eileen", predicate="status", object_value="on a secret errand",
                 status="active", confidence=0.9):
        self.id = "fact_1"
        self.subject_id = subject_id
        self.predicate = predicate
        self.object_value = object_value
        self.status = status
        self.confidence = confidence
        self.observer_ids = ["duke", "eileen"]


class _Store:
    def __init__(self, facts):
        self._facts = list(facts)

    def find_facts(self, campaign_id, **kwargs):
        return list(self._facts)

    def list_entities(self, campaign_id):
        return []


def _engine(facts, minds):
    return MemoryEngine(event_store=None, llm=None, fact_store=_Store(facts), minds=minds)


class TestReadingBeliefsBack:
    def test_a_told_belief_becomes_a_claim(self):
        minds = _Minds({"duke": _Mind({"belief:status:Eileen": _Thought("dead")})})
        engine = _engine([_Fact()], minds)
        claims = engine.stored_beliefs("c1", "duke")
        assert len(claims) == 1
        claim = claims[0]
        assert (claim.subject_id, claim.predicate, claim.object_value) == ("Eileen", "status", "dead")
        assert claim.stance == BELIEVES

    def test_ordinary_thoughts_are_not_claims(self):
        minds = _Minds({"duke": _Mind({"mood": _Thought("angry"), "goal": _Thought("revenge")})})
        engine = _engine([_Fact()], minds)
        assert engine.stored_beliefs("c1", "duke") == []

    def test_a_malformed_key_is_skipped_not_crashed(self):
        minds = _Minds({"duke": _Mind({"belief:status": _Thought("dead"), "belief::x:y": _Thought("")})})
        engine = _engine([_Fact()], minds)
        assert engine.stored_beliefs("c1", "duke") == []

    def test_without_a_mind_store_there_are_no_beliefs(self):
        engine = MemoryEngine(event_store=None, llm=None)
        assert engine.stored_beliefs("c1", "duke") == []

    def test_a_broken_mind_store_degrades_quietly(self):
        class _Broken:
            def get_mind(self, campaign_id, npc_name):
                raise RuntimeError("mind database is gone")

        engine = _engine([_Fact()], _Broken())
        assert engine.stored_beliefs("c1", "duke") == []


class TestTheDivergenceReachesThePrompt:
    def test_the_perspective_marks_the_fact_as_mistaken(self):
        minds = _Minds({"duke": _Mind({"belief:status:Eileen": _Thought("dead")})})
        engine = _engine([_Fact()], minds)
        perspective = engine.build_npc_perspective("c1", "duke")
        assert perspective is not None
        assert "fact_1" in perspective.mistaken_fact_ids
        assert perspective.believes(_Fact()) is not None

    def test_the_prompt_shows_the_wrong_version_to_the_wrong_person(self):
        minds = _Minds({"duke": _Mind({"belief:status:Eileen": _Thought("dead")})})
        engine = _engine([_Fact()], minds)
        perspective = engine.build_npc_perspective("c1", "duke")
        lines = facts_for_prompt([_Fact()], observer="duke", perspective=perspective)
        assert any("heard, may be wrong" in line and "dead" in line for line in lines
                   ), lines
        assert not any("secret errand" in line for line in lines), lines

    def test_the_person_who_knows_gets_the_truth(self):
        minds = _Minds({"eileen": _Mind({})})
        engine = _engine([_Fact()], minds)
        perspective = engine.build_npc_perspective("c1", "eileen")
        lines = facts_for_prompt([_Fact()], observer="eileen", perspective=perspective)
        assert any("(fact)" in line and "secret errand" in line for line in lines), lines

    def test_a_belief_that_matches_the_world_is_downgraded_to_knowing(self):
        minds = _Minds({"eileen": _Mind({"belief:status:Eileen": _Thought("on a secret errand")})})
        engine = _engine([_Fact()], minds)
        perspective = engine.build_npc_perspective("c1", "eileen")
        assert perspective.mistaken_fact_ids == []
        assert perspective.claims[0].stance == KNOWS
        assert "heard" not in perspective.claims[0].render()

    def test_a_stranger_sees_nothing_private(self):
        minds = _Minds({"stranger": _Mind({})})
        engine = _engine([_Fact()], minds)
        perspective = engine.build_npc_perspective("c1", "stranger")
        # observer_ids lists duke and eileen only; the stranger is not admitted.
        assert perspective.known_fact_ids == []
        lines = facts_for_prompt([_Fact()], observer="stranger", perspective=perspective)
        assert lines == []

class TestWritingABelief:
    """The write path, so a rumour does not have to be typed into a dict."""

    class _MindStore:
        def __init__(self):
            self.minds = {}

        def ensure_mind(self, campaign_id, npc_name, current_turn=0):
            return self.minds.setdefault(npc_name, _Mind())

        def get_mind(self, campaign_id, npc_name):
            return self.minds.get(npc_name)

    def test_a_rumour_lands_in_the_npc_own_mind(self):
        store = self._MindStore()
        key = remember_belief(
            store, "c1", "duke",
            subject_id="Eileen", predicate="status", object_value="dead", turn=4,
        )
        assert key == "belief:status:Eileen"
        thoughts = store.minds["duke"].thoughts
        assert thoughts[key].value == "dead"
        # A belief must not decay away after a few turns.
        assert thoughts[key].decay_after_turns == 0

    def test_an_empty_object_is_not_a_claim(self):
        store = self._MindStore()
        assert remember_belief(store, "c1", "duke", subject_id="Eileen", predicate="status", object_value="  ") == ""
        assert store.minds == {}

    def test_no_mind_store_is_not_an_error(self):
        assert remember_belief(None, "c1", "duke", subject_id="Eileen", predicate="status", object_value="dead") == ""

    def test_a_colon_in_a_name_does_not_split_the_key(self):
        key = belief_key("Lord:Vane", "status")
        assert key.count(":") == 2
        assert key.startswith("belief:status:")

    def test_the_written_belief_is_read_back_as_a_claim(self):
        store = self._MindStore()
        remember_belief(store, "c1", "duke", subject_id="Eileen", predicate="status", object_value="dead")
        engine = _engine([_Fact()], store)
        perspective = engine.build_npc_perspective("c1", "duke")
        assert perspective is not None
        assert perspective.claims[0].object_value == "dead"
        lines = facts_for_prompt([_Fact()], observer="duke", perspective=perspective)
        assert any("dead" in line for line in lines)
        assert not any("secret errand" in line for line in lines)
