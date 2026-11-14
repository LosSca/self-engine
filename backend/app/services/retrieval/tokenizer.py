"""One tokenizer for English and Chinese (and anything that writes in
space-separated words).

The old lexical layers disagreed: `knowledge_service` tokenized CJK into
characters plus bigrams, while `memory_engine` matched only `[a-zA-ZÀ-ÿ]{3,}`
- so a Chinese query produced an empty keyword set and every crystal scored the
same. One tokenizer removes that class of bug and gives BM25 a consistent term
space.

Rules:

* Latin/Cyrillic/Greek words are casefolded and split on anything that is not a
  letter or digit; a trailing possessive is dropped.
* CJK runs are cut into bigrams (the smallest unit that carries meaning in
  Chinese without a dictionary), and a run of one character yields that
  character. Single characters are not emitted alongside bigrams: doing so
  doubles the index size for no extra signal.
* Digits are kept as tokens: "3 天" and "3天" should match.
* A stop list in each supported language is removed, but only the stop words of
  the scripts that actually appear in the text.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

_LATIN_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CJK_RUN_RE = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]+"
)
_PUNCT_CATEGORY_PREFIX = "P"
_SYMBOL_CATEGORY_PREFIX = "S"

# English function words. Kept short on purpose: an over-long stop list hurts
# recall on a domain where "in", "at" and "on" can name a location.
_STOP_WORDS_EN = frozenset(
    """
    the a an and or but if then than that this these those there here
    is are was were be been being am do does did doing have has had having
    of in on at to from by for with without about into over under again
    as so such no not only own same too very can will just should now
    i you he she it we they me him her us them my your his her its our their
    """.split()
)

# Chinese function words, plus the two-character forms that JS/JSON payloads
# produce when a Chinese crystal is written through the narrator.
_STOP_BIGRAMS_ZH = frozenset(
    """
    什么 一个 这个 那个 我们 你们 他们 她们 它们 自己 已经 可以 因为 所以
    但是 而且 如果 虽然 然后 就是 还是 只是 这里 那里 现在 之前 之后 以后
    一下 一些 没有 不是 不能 可能 应该 需要 进行 通过 关于 对于 由于 以及
    """.split()
)
_STOP_CHARS_ZH = frozenset("的了着过和与及或在是有不也都就很还把被将给对从向往为")


@dataclass(frozen=True)
class TokenizeConfig:
    """Switches that a caller may turn off when it needs raw tokens.

    Token-count-aware rerankers want the bigrams but not the stop list; an
    index rebuild wants both. Defaults match what retrieval uses.
    """

    remove_stop_words: bool = True
    min_length: int = 1
    max_length: int = 40
    drop_pure_digits: bool = False


DEFAULT_TOKENIZE_CONFIG = TokenizeConfig()


@dataclass(frozen=True)
class TokenBatch:
    """Tokens plus the script each token came from.

    `has_cjk` lets idiomatic code branch on "is this a Chinese query" without
    re-running a regex, and `normalized` keeps the case-folded text so a
    phrase-based reranker can look for exact substrings.
    """

    tokens: List[str] = field(default_factory=list)
    has_cjk: bool = False
    has_latin: bool = False
    normalized: str = ""

    def __len__(self) -> int:
        return len(self.tokens)


def tokenize(text: str, config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG) -> TokenBatch:
    """Split mixed-script text into indexable tokens (see module docstring)."""
    if not text:
        return TokenBatch(normalized="")
    normalized = unicodedata.normalize("NFKC", str(text)).casefold()
    tokens: List[str] = []
    has_cjk = False
    has_latin = False

    for run in _iter_runs(normalized):
        if _CJK_RUN_RE.fullmatch(run):
            has_cjk = True
            tokens.extend(_cjk_tokens(run, config))
        else:
            has_latin = True
            tokens.extend(_latin_tokens(run, config))

    if config.remove_stop_words:
        tokens = [t for t in tokens if not _is_stop_word(t)]
    return TokenBatch(tokens=tokens, has_cjk=has_cjk, has_latin=has_latin, normalized=normalized)


def tokenize_text(text: str, config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG) -> List[str]:
    """Just the tokens, for callers that do not need the script flags."""
    return tokenize(text, config).tokens


def token_set(text: str, config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG) -> set:
    """Token set, for overlap tests (crystal scoring, focus matching)."""
    return set(tokenize(text, config).tokens)


def _iter_runs(text: str) -> Iterable[str]:
    """Alternate between CJK runs and everything else, without losing order."""
    pos = 0
    while pos < len(text):
        match = _CJK_RUN_RE.search(text, pos)
        if match is None:
            tail = text[pos:]
            if tail.strip():
                yield tail
            return
        head = text[pos : match.start()]
        if head.strip():
            yield head
        yield match.group(0)
        pos = match.end()


def _is_separator(char: str) -> bool:
    category = unicodedata.category(char)
    return category.startswith(_PUNCT_CATEGORY_PREFIX) or category.startswith(_SYMBOL_CATEGORY_PREFIX)


def _latin_tokens(run: str, config: TokenizeConfig) -> List[str]:
    out: List[str] = []
    for word in _LATIN_WORD_RE.findall(run):
        if word.endswith("'s") or word.endswith("’s"):
            word = word[:-2]
        if not word:
            continue
        if config.drop_pure_digits and word.isdigit():
            continue
        if not (config.min_length <= len(word) <= config.max_length):
            continue
        out.append(word)
    return out


def _cjk_tokens(run: str, config: TokenizeConfig) -> List[str]:
    if len(run) < 2:
        return [run] if config.min_length <= 1 <= config.max_length else []
    bigrams = [run[i : i + 2] for i in range(len(run) - 1)]
    return [b for b in bigrams if config.min_length <= len(b) <= config.max_length]


def _is_stop_word(token: str) -> bool:
    if token in _STOP_WORDS_EN or token in _STOP_BIGRAMS_ZH:
        return True
    if len(token) == 1 and token in _STOP_CHARS_ZH:
        return True
    return False


def has_cjk(text: str) -> bool:
    """True when the text contains any CJK character (no tokenizing involved)."""
    return bool(_CJK_RUN_RE.search(unicodedata.normalize("NFKC", str(text or ""))))


def normalize_query(text: str) -> str:
    """Case-folded, whitespace-collapsed text used for exact-phrase checks."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(normalized.split())


def tokenize_many(texts: Sequence[str], config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG) -> List[List[str]]:
    """Tokenize a corpus in one call; the vector layer reuses it for caching."""
    return [tokenize_text(text, config) for text in texts]
