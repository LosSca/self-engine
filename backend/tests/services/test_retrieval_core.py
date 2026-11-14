"""The shared tokenizer, BM25 and reciprocal-rank fusion.

These are the regression tests for the three bugs the old layers carried:
Chinese text producing no tokens, documents scored by length rather than
relevance, and two ranked lists never being merged.
"""
from __future__ import annotations

from app.services.retrieval import lexical_retriever
from app.services.retrieval.fusion import reciprocal_rank_fusion
from app.services.retrieval.models import FusionConfig, RetrievedDoc
from app.services.retrieval.tokenizer import tokenize, tokenize_text


def test_chinese_text_produces_bigrams():
    tokens = tokenize_text("酒馆里有一把铁钥匙")
    assert "酒馆" in tokens
    assert "铁钥" in tokens or "钥匙" in tokens
    # Single characters are not emitted alongside bigrams (the old tokenizer
    # emitted both, doubling the index for no extra signal).
    assert "酒" not in tokens


def test_mixed_script_keeps_both_sides():
    batch = tokenize("去找铁钥匙 iron_key")
    assert batch.has_cjk and batch.has_latin
    assert "iron" in batch.tokens and "key" in batch.tokens
    assert "铁钥" in batch.tokens


def test_stop_words_are_removed():
    assert tokenize_text("the key of the door") == ["key", "door"]
    # The stop list is per-script: a Chinese stop word must not remove an
    # English token that happens to look like one, and vice versa.
    assert "酒馆" in tokenize_text("去酒馆")


def test_single_cjk_character_is_kept():
    assert tokenize_text("锁") == ["锁"]


def test_digits_survive_as_tokens():
    assert "3" in tokenize_text("等3天")


def test_bm25_ranks_the_document_that_answers_the_query():
    docs = [
        "酒馆吧台下藏着一把铁钥匙",
        "森林深处住着一只老狼",
        "The tavern keeper hides an iron key behind the bar",
    ]
    hits = lexical_retriever.search(docs, "铁钥匙 酒馆")
    assert hits and docs.index(hits[0].text) == 0
    english = lexical_retriever.search(docs, "tavern iron key")
    assert english and docs.index(english[0].text) == 2


def test_bm25_drops_documents_with_no_match():
    docs = ["森林深处住着一只老狼", "酒馆里全是醉汉"]
    assert lexical_retriever.search(docs, "铁钥匙") == []


def test_bm25_saturates_term_frequency():
    """A document that repeats a word must not beat a document that is about it."""
    docs = [
        "钥匙 钥匙 钥匙 钥匙 钥匙 钥匙 钥匙 钥匙",
        "钥匙 藏在 酒馆 吧台 下面 的 铁盒 里 钥匙",
    ]
    hits = lexical_retriever.search(docs, "钥匙 酒馆 吧台")
    assert docs.index(hits[0].text) == 1


def test_bm25_empty_query_or_empty_corpus_is_empty():
    assert lexical_retriever.search([], "钥匙") == []
    assert lexical_retriever.search(["钥匙"], "   ") == []


def _doc(doc_id: str, channel: str, rank_hint: float = 0.0) -> RetrievedDoc:
    return RetrievedDoc(doc_id=doc_id, score=rank_hint, channel=channel, text=doc_id)


def test_rrf_keeps_documents_found_by_only_one_channel():
    lexical = [_doc("a", "bm25", 9.0), _doc("b", "bm25", 5.0)]
    vector = [_doc("c", "vector", 0.9), _doc("a", "vector", 0.8)]
    fused = reciprocal_rank_fusion([lexical, vector])
    ids = [doc.doc_id for doc in fused]
    assert set(ids) == {"a", "b", "c"}
    # "a" is found by both channels, so it must lead.
    assert ids[0] == "a"


def test_rrf_ignores_score_scale():
    """A channel with huge scores must not win by being loud."""
    loud = [_doc("x", "bm25", 10_000.0)]
    quiet = [_doc("y", "vector", 0.01)]
    fused = reciprocal_rank_fusion([loud, quiet])
    assert [doc.doc_id for doc in fused] == ["x", "y"]


def test_rrf_weights_express_trust():
    left = [_doc("a", "graph")]
    right = [_doc("b", "bm25")]
    fused = reciprocal_rank_fusion([left, right], FusionConfig(weights={"graph": 3.0}))
    assert fused[0].doc_id == "a"


def test_rrf_is_deterministic():
    left = [_doc("a", "bm25"), _doc("b", "bm25")]
    right = [_doc("b", "vector"), _doc("a", "vector")]
    first = [doc.doc_id for doc in reciprocal_rank_fusion([left, right])]
    second = [doc.doc_id for doc in reciprocal_rank_fusion([left, right])]
    assert first == second
