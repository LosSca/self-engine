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
