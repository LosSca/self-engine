"""One tokenizer for every ranking path.

Three retrieval paths rank text against a scene: memory crystal recall
(memory_engine._extract_keywords), knowledge retrieval
(knowledge_service._tokens) and story-card relevance
(GameSession._extract_context_keywords). They used to disagree: one matched
only Latin letters, so a Chinese query produced no keywords at all and every
candidate scored the same.

These tests pin the shared behavior, especially for Chinese and Japanese,
which is what the game is actually played in.
"""
from types import SimpleNamespace

import pytest

from app.engines.memory_engine import _extract_keywords
from app.services.game_session import GameSession
from app.services.knowledge_service import _tokens
from app.services.retrieval.tokenizer import has_cjk, tokenize_text


def _session():
    """A GameSession without __init__: only class-level constants are read."""
    return object.__new__(GameSession)


# ── the same text tokenizes the same way everywhere ─────────────────


@pytest.mark.parametrize(
    "text",
    [
        "The Dark Forest at Night",
        "上次救过我的女骑士现在怎么样了",
        "エリンは北の門を守っている",
        "Eileen 艾琳 エリン - mixed 混合 query",
        "",
    ],
)
def test_all_three_paths_share_one_term_space(text):
    shared = set(tokenize_text(text))
    assert set(_tokens(text)) == shared
    # Memory recall additionally drops its stop list, never adds terms...
    assert _extract_keywords(text) <= shared
    # ...and story cards keep the same subset plus the CJK exemption.
    assert GameSession._extract_context_keywords(text) <= shared


# ── Chinese ─────────────────────────────────────────────────────────


def test_a_chinese_query_has_keywords():
    keywords = _extract_keywords("上次救过我的女骑士现在怎么样了")
    assert keywords, "a Chinese query must not tokenize to nothing"
    assert any(has_cjk(token) for token in keywords)
    # Bigrams are what make Chinese retrieval work without a segmenter.
    assert "女骑" in keywords or "骑士" in keywords


def test_chinese_bigrams_are_not_dropped_as_short_tokens():
    # "the" is 3 characters and stops; 森林 is 2 characters and must not.
    keywords = GameSession._extract_context_keywords("我走进黑森林遇见了艾琳")
    assert "森林" in keywords
    assert "艾琳" in keywords


def test_english_stopwords_do_not_become_keywords():
    keywords = _extract_keywords("The knight of the north gate")
    assert "the" not in keywords
    assert "knight" in keywords and "gate" in keywords


def test_short_latin_fragments_are_filtered_from_scene_keywords():
    # The old rule was len > 2; it still applies to Latin text only.
    keywords = GameSession._extract_context_keywords("go to the inn")
    assert "go" not in keywords
    assert "inn" in keywords


# ── Japanese ────────────────────────────────────────────────────────


def test_a_japanese_query_has_keywords():
    keywords = _extract_keywords("エリンは北の門を守っている")
    assert keywords
    assert any(has_cjk(token) for token in keywords)


def test_japanese_kanji_and_kana_mix():
    keywords = _extract_keywords("黒い森で騎士に会った")
    assert any(has_cjk(token) for token in keywords)
    assert "黒い" in keywords or "森で" in keywords or "騎士" in keywords


# ── aliases and mixed script ────────────────────────────────────────


def test_mixed_script_query_keeps_both_sides():
    keywords = _tokens("艾琳 Eileen エリン")
    assert "eileen" in keywords
    assert "艾琳" in keywords or any(has_cjk(token) and "艾琳" in token for token in keywords)


def test_names_are_normalized_case_insensitively():
    assert "eileen" in _tokens("EILEEN")
    assert "eileen" in _tokens("Eileen")


# ── story cards ────────────────────────────────────────────────────


def test_a_chinese_card_matches_a_chinese_scene():
    """The regression that made this fix necessary: whitespace splitting a
    Chinese card yields one long token and never overlaps the scene."""
    session = _session()
    card = SimpleNamespace(
        name="黑森林",
        content={"desc": "古老而危险的森林，夜里常有狼群"},
        card_type="LOCATION",
    )
    scene_keywords = GameSession._extract_context_keywords("我走进黑森林，听见狼群嚎叫")
    score = GameSession._score_card_relevance(
        session, card, scene_keywords, set(), set()
    )
    assert score > 0, "a Chinese card must score against a Chinese scene"

    # A whitespace split on the same card would have scored zero.
    naive = {word for word in card.name.lower().split()}
    assert not (scene_keywords & naive)


def test_an_unrelated_card_scores_zero():
    session = _session()
    card = SimpleNamespace(name="Harbor Ledger", content={"desc": "shipping manifest"}, card_type="ITEM")
    scene_keywords = GameSession._extract_context_keywords("我走进黑森林")
    assert GameSession._score_card_relevance(
        session, card, scene_keywords, set(), set()
    ) == 0.0
