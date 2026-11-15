"""Language codes used by scenarios, the UI and prompt construction.

Scenarios store a short code (`zh`, `en`). Prompts need a human-readable
language name, and older data may still carry codes such as `pt-br`, so
unknown codes pass through unchanged instead of failing.
"""
from __future__ import annotations

LANGUAGE_NAMES: dict[str, str] = {
    "zh": "Simplified Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-hans": "Simplified Chinese",
    "zh-hant": "Traditional Chinese",
    "en": "English",
    "en-us": "English",
    "en-gb": "English",
    "pt-br": "Brazilian Portuguese",
    "pt": "Portuguese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
    "ko": "Korean",
}

SUPPORTED_LANGUAGES = ("zh", "en")
DEFAULT_LANGUAGE = "zh"


def normalize_language(code: str | None) -> str:
    """Map any stored/typed code onto one of the supported UI codes."""
    if not code:
        return DEFAULT_LANGUAGE
    c = code.strip().lower()
    if c in ("zh", "zh-cn", "zh-hans", "zh-hant", "cn", "chinese"):
        return "zh"
    if c in ("en", "en-us", "en-gb", "english"):
        return "en"
    return c


def language_name(code: str | None, default: str = "Simplified Chinese") -> str:
    """Human language name for prompts."""
    if not code:
        return default
    return LANGUAGE_NAMES.get(code.strip().lower(), code)


def detect_language(world) -> str:
    """The language a world's prose should be written in.

    `WorldDefinition.language` is optional and defaults to an empty string,
    which most authored worlds leave untouched. Treating "" as English made
    every Chinese world speak English, so an unset field is inferred from the
    world's own text instead: the majority script among the names and
    descriptions the player will actually read.

    Ties fall back to the default language rather than to English, because the
    default is what an author who never set the field expects to see.
    """
    explicit = normalize_language(getattr(world, "language", "") or "")
    raw = (getattr(world, "language", "") or "").strip()
    if raw:
        return explicit
    samples: list[str] = [
        str(getattr(world, "name", "") or ""),
        str(getattr(world, "description", "") or ""),
    ]
    for collection in ("locations", "items", "entities", "quests", "doors"):
        for entry in getattr(world, collection, []) or []:
            samples.append(str(getattr(entry, "name", "") or ""))
            samples.append(str(getattr(entry, "description", "") or ""))
    cjk = sum(1 for ch in "".join(samples) if _is_cjk(ch))
    latin = sum(1 for ch in "".join(samples) if ch.isascii() and ch.isalpha())
    if not cjk and not latin:
        return DEFAULT_LANGUAGE
    if cjk >= latin:
        return "zh"
    return "en"


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3040 <= code <= 0x30FF      # kana
        or 0x3400 <= code <= 0x4DBF   # CJK ext A
        or 0x4E00 <= code <= 0x9FFF   # CJK unified
        or 0xF900 <= code <= 0xFAFF   # compatibility
        or 0xAC00 <= code <= 0x7DAF   # hangul
    )
