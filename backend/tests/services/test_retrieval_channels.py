"""Semantic channel, graph channel and the evaluation harness.

The failure mode under test throughout is the old one: something optional was
unavailable and the code said nothing. Every channel here must either answer or
report why it did not.
"""
from __future__ import annotations

import os

import pytest

from app.services.retrieval.embedding_index import EmbeddingCache, build_index
from app.services.retrieval.embedding_provider import (
    EmbeddingConfig,
    EmbeddingProvider,
    EmbeddingUnavailable,
    normalise,
)
from app.services.retrieval.evaluation import EvalCase, compare, evaluate
from app.services.retrieval.graph_query import (
    build_graph_query,
    filter_valid,
    is_fact_valid,
    order_by_recency,
    resolve_aliases,
)
from app.services.retrieval.graph_retriever import GraphRetriever
from app.services.retrieval.query_builder import build_graph_query as build_query_parts
from app.services.retrieval.vector_retriever import VectorRetriever


class _StubProvider(EmbeddingProvider):
    """Deterministic toy space: (key/thief, tavern, forest) axes."""

    name = "stub"

    def available(self) -> bool:
        return True

    def _embed_batch(self, texts):
        out = []
        for text in texts:
            low = text.lower()
            out.append(
                normalise(
                    [
                        1.0 if ("钥匙" in low or "key" in low) else 0.0,
                        1.0 if ("酒馆" in low or "tavern" in low) else 0.0,
                        1.0 if ("森林" in low or "forest" in low) else 0.0,
                    ]
                )
            )
        return out


class _BrokenProvider(_StubProvider):
    def _embed_batch(self, texts):
        raise EmbeddingUnavailable("endpoint down")


def test_vector_channel_finds_a_document_with_no_shared_words():
    docs = ["吧台下藏着一把铁钥匙", "森林深处住着一只老狼", "酒馆老板认识每一个常客"]
    channel = VectorRetriever(_StubProvider(), docs, ["k0", "k1", "k2"])
    hits = channel.search("key")
    assert hits and hits[0].doc_id == "k0"
    assert hits[0].channel == "vector"


def test_vector_channel_degrades_instead_of_raising():
    docs = ["吧台下藏着一把铁钥匙"]
    channel = VectorRetriever(_BrokenProvider(), docs, ["k0"])
    assert channel.search("key") == []
    assert channel.error  # the reason is recorded, not swallowed


def test_vector_channel_returns_nothing_for_an_empty_query():
    channel = VectorRetriever(_StubProvider(), ["a"], ["k0"])
    assert channel.search("   ") == []


def test_embedding_cache_reuses_vectors_and_reports_counts(tmp_path):
    cache = EmbeddingCache(str(tmp_path / "emb.db"))
    provider = _StubProvider(EmbeddingConfig(provider="stub", model="stub-v1"))
    docs = ["钥匙", "酒馆"]
    first = build_index(provider, docs, ["a", "b"], cache=cache)
    assert first.embedded == 2 and first.cached == 0
    second = build_index(provider, docs, ["a", "b"], cache=cache)
    assert second.embedded == 0 and second.cached == 2
    assert cache.count(provider.fingerprint) == 2


def test_embedding_cache_is_invalidated_by_a_model_change(tmp_path):
    cache = EmbeddingCache(str(tmp_path / "emb.db"))
    first = _StubProvider(EmbeddingConfig(provider="stub", model="m1"))
    second = _StubProvider(EmbeddingConfig(provider="stub", model="m2"))
    assert first.fingerprint != second.fingerprint
    build_index(first, ["钥匙"], ["a"], cache=cache)
    rebuilt = build_index(second, ["钥匙"], ["a"], cache=cache)
    assert rebuilt.embedded == 1  # re-embedded: never mix two vector spaces


def test_graph_query_names_entities_not_paragraphs():
    parts = build_query_parts(
        location="酒馆", active_npcs=["老巴克"], player_input="我推开门看看"
    )
    assert "老巴克" in parts.text()
    assert "酒馆" in parts.text()
    assert "推开门" not in parts.text()


def test_graph_query_falls_back_to_the_player_input_without_entities():
    parts = build_query_parts(player_input="去看看外面的情况")
    assert "情况" in parts.text() or "外面" in parts.text()


def test_alias_resolution_folds_a_short_name_onto_the_canonical_one():
    assert resolve_aliases(["巴克"], ["老巴克", "铁匠汉森"]) == ["老巴克"]
    assert resolve_aliases(["Bark"], ["老巴克"], {"bark": "老巴克"}) == ["老巴克"]


def test_graph_query_builder_uses_the_roster():
    query = build_graph_query(
        "他走进酒馆，和巴克打了个招呼",
        known_names=["老巴克", "铁匠汉森"],
    )
    assert "老巴克" in query.entities


def test_expired_facts_are_dropped_but_history_can_be_asked_for():
    facts = [
        {"fact": "old", "invalid_at": "2026-02-01T00:00:00Z"},
        {"fact": "current", "invalid_at": None},
    ]
    assert [f["fact"] for f in filter_valid(facts)] == ["current"]
    assert len(filter_valid(facts, include_invalid=True)) == 2


def test_fact_validity_handles_unparsable_timestamps():
    assert is_fact_valid({"fact": "x", "invalid_at": "not a date"}) is True


def test_facts_are_ordered_newest_first():
    facts = [
        {"fact": "old", "valid_at": "2026-01-01T00:00:00Z"},
        {"fact": "new", "valid_at": "2026-05-01T00:00:00Z"},
        {"fact": "undated"},
    ]
    assert [f["fact"] for f in order_by_recency(facts)] == ["new", "old", "undated"]


class _FakeGraphEngine:
    def __init__(self, facts):
        self._facts = facts
        self.queries = []

    async def search(self, campaign_id, query, limit=10):
        self.queries.append(query)
        return [dict(fact) for fact in self._facts]


@pytest.mark.asyncio
async def test_graph_retriever_ranks_connected_facts_first():
    engine = _FakeGraphEngine(
        [
            {"fact": "老巴克 owns the iron key", "valid_at": "2026-04-01T00:00:00Z"},
            {"fact": "the forest is quiet", "valid_at": "2026-04-02T00:00:00Z"},
        ]
    )
    retriever = GraphRetriever(engine, "c1", known_names=["老巴克"])
    query = build_graph_query("老巴克 铁钥匙", known_names=["老巴克"])
    hits = await retriever.search_async(query)
    assert hits and "老巴克" in hits[0].text


@pytest.mark.asyncio
async def test_graph_retriever_survives_an_engine_failure():
    class _Dead:
        async def search(self, *args, **kwargs):
            raise OSError("neo4j is down")

    retriever = GraphRetriever(_Dead(), "c1")
    query = build_graph_query("老巴克")
    assert await retriever.search_async(query) == []
    assert retriever.error


def test_evaluation_metrics_reward_putting_the_right_answer_first():
    cases = [EvalCase(query="钥匙", relevant=["k0"])]
    good = evaluate(
        "good", cases, lambda case: [_doc("k0"), _doc("k1")], k=2, label_source="hand"
    )
    bad = evaluate(
        "bad", cases, lambda case: [_doc("k1"), _doc("k0")], k=2, label_source="hand"
    )
    assert good.mean("mrr") == 1.0
    assert bad.mean("mrr") == 0.5
    assert good.mean("ndcg") > bad.mean("ndcg")
    rows = compare([good, bad], metric="mrr")
    assert rows[0]["name"] == "good"


def _doc(doc_id: str, channel: str = "bm25"):
    from app.services.retrieval.models import RetrievedDoc

    return RetrievedDoc(doc_id=doc_id, score=1.0, channel=channel, text=doc_id)
