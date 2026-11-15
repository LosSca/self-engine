"""The semantic channel is optional, and its absence must be invisible.

Two things are being pinned here:

* With no embedding backend configured (the only state this project ships
  in), memory recall behaves exactly as it did before the channel existed -
  no crash, no empty window, no silently degraded ordering.
* When a backend *is* present, the channel is a real channel: it can add a
  crystal no keyword found, RRF keeps the keyword ranking meaningful, and a
  backend that raises mid-turn is reported as degraded instead of taking the
  turn down.
"""
from __future__ import annotations

import pytest

from app.services.retrieval.memory_retrieval import (
    CHANNEL_VECTOR,
    CrystalDoc,
    build_crystal_vector_channel,
    hybrid_rank,
)
from app.services.retrieval.models import RetrievedDoc


def _doc(doc_id: str, text: str, **kwargs) -> CrystalDoc:
    return CrystalDoc(doc_id=doc_id, text=text, **kwargs)


class _FakeVector:
    """Stands in for a VectorRetriever: returns whatever it is told to."""

    def __init__(self, hits):
        self._hits = hits
        self.queries = []

    def search(self, query: str):
        self.queries.append(query)
        return [
            RetrievedDoc(
                doc_id=doc_id,
                score=score,
                channel=CHANNEL_VECTOR,
                text=f"text of {doc_id}",
                metadata={},
                provenance="cos:test",
            )
            for doc_id, score in self._hits
        ]


class _BrokenVector:
    def search(self, query: str):
        raise RuntimeError("embedding backend went away")


class TestWithoutABackend:
    def test_no_channel_when_the_builder_is_absent(self):
        # build_vector_channel returns None when no provider is configured;
        # the memory layer must treat that as "no channel", not as an error.
        assert build_crystal_vector_channel([], builder=lambda *a, **k: None) is None

    def test_no_channel_for_an_empty_corpus(self):
        assert build_crystal_vector_channel([], builder=lambda *a, **k: object()) is None

    def test_hybrid_rank_is_unchanged_when_the_channel_is_absent(self):
        docs = [_doc("a", "the knight saved you"), _doc("b", "you bought bread")]
        without = hybrid_rank("knight", docs)
        with_none = hybrid_rank("knight", docs, vector=None)
        assert without.doc_ids() == with_none.doc_ids()
        assert without.channels == with_none.channels

    def test_a_builder_that_raises_yields_no_channel(self):
        def broken(*args, **kwargs):
            raise ImportError("no model stack here")

        assert build_crystal_vector_channel([_doc("a", "text")], builder=broken) is None

    def test_engine_survives_a_missing_embedding_backend(self, monkeypatch):
        import app.services.retrieval_embedding as embedding

        monkeypatch.setattr(embedding, "provider", lambda: None)
        channel = build_crystal_vector_channel([_doc("a", "the knight saved you")])
        assert channel is None


class TestWithABackend:
    def test_the_channel_recalls_a_crystal_no_keyword_matched(self):
        # "that lady who pulled me out of the fire" shares no token with the
        # crystal, which is exactly the case the semantic channel exists for.
        docs = [
            _doc("fire", "Eileen dragged you from the burning barn"),
            _doc("bread", "you bought bread at the market"),
        ]
        vector = _FakeVector([("fire", 0.91), ("bread", 0.12)])
        result = hybrid_rank("that lady who pulled me out of the fire", docs, vector=vector)
        assert "fire" in result.doc_ids()
        assert result.channels.get(CHANNEL_VECTOR, 0) == 2

    def test_keyword_evidence_still_outranks_a_lone_cosine_hit(self):
        # Same cosine for both, but one of them actually contains the word.
        docs = [
            _doc("exact", "the knight keeps watch at the north gate"),
            _doc("vague", "someone was standing around"),
        ]
        vector = _FakeVector([("vague", 0.80), ("exact", 0.80)])
        result = hybrid_rank("north gate", docs, vector=vector)
        assert result.doc_ids()[0] == "exact"

    def test_a_failing_channel_is_reported_not_raised(self):
        docs = [_doc("a", "the knight saved you")]
        result = hybrid_rank("knight", docs, vector=_BrokenVector())
        assert result.doc_ids() == ["a"]
        assert CHANNEL_VECTOR in result.degraded
        assert any("RuntimeError" in error for error in result.errors)

    def test_the_query_reaches_the_channel_verbatim(self):
        vector = _FakeVector([("a", 0.5)])
        hybrid_rank("she is the one who rescued me", [_doc("a", "irrelevant words")], vector=vector)
        assert vector.queries == ["she is the one who rescued me"]


class TestEngineWiring:
    def test_the_engine_builds_the_channel_once_per_corpus(self, monkeypatch):
        import app.engines.memory_engine as engine_module

        built = []

        def fake_build(docs, *, builder=None):
            built.append([doc.doc_id for doc in docs])
            return _FakeVector([(docs[0].doc_id, 0.5)])

        monkeypatch.setattr(engine_module, "build_crystal_vector_channel", fake_build)
        engine = engine_module.MemoryEngine(event_store=None, llm=None)
        docs = [_doc("a", "the knight saved you"), _doc("b", "bread")]
        first = engine._vector_channel("campaign", docs)
        second = engine._vector_channel("campaign", docs)
        assert first is second
        assert len(built) == 1

    def test_invalidating_forgets_the_channel(self, monkeypatch):
        import app.engines.memory_engine as engine_module

        built = []

        def fake_build(docs, *, builder=None):
            built.append(1)
            return _FakeVector([(docs[0].doc_id, 0.5)])

        monkeypatch.setattr(engine_module, "build_crystal_vector_channel", fake_build)
        engine = engine_module.MemoryEngine(event_store=None, llm=None)
        docs = [_doc("a", "the knight saved you")]
        engine._vector_channel("campaign", docs)
        engine.invalidate_vector_cache()
        engine._vector_channel("campaign", docs)
        assert len(built) == 2

    def test_no_channel_means_none_not_an_error(self, monkeypatch):
        import app.engines.memory_engine as engine_module

        monkeypatch.setattr(engine_module, "build_crystal_vector_channel", lambda docs, **kw: None)
        engine = engine_module.MemoryEngine(event_store=None, llm=None)
        assert engine._vector_channel("campaign", [_doc("a", "text")]) is None
