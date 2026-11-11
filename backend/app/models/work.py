"""A work of fiction the player wants to enter or borrow a role from.

Profiles are advisory text for prompts and pickers.  Nothing here is game
state: a profile can name a character's abilities, but only the rules engine
decides what an action can actually do.
"""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CharacterProfile(_Strict):
    """One pickable role from a work.  Description only, never game state."""

    name: str = Field(..., min_length=1, max_length=80)
    role: str = Field("", max_length=120)
    description: str = Field("", max_length=2000)
    traits: List[str] = Field(default_factory=list)
    source_work: str = Field("", max_length=200)


class WorkProfile(_Strict):
    """Everything the generator needs to seed a campaign from one work."""

    title: str = Field(..., min_length=1, max_length=200)
    medium: str = Field("novel", max_length=40)
    summary: str = Field("", max_length=2000)
    tone: str = Field("", max_length=400)
    characters: List[CharacterProfile] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=list)
    #: Optional one-line description per location, index-aligned with `locations`.
    location_notes: List[str] = Field(default_factory=list)
    opening_hint: str = Field("", max_length=1000)
    source: str = "curated"

    @classmethod
    def from_payload(cls, payload: Any, title: str = "", source: str = "generated") -> "WorkProfile":
        """Validate model output into a profile, filling in what it omitted."""
        if not isinstance(payload, dict):
            raise ValueError("模型没有返回作品对象")
        data = dict(payload)
        data["title"] = str(data.get("title") or title or "").strip()
        if not data["title"]:
            raise ValueError("作品名为空")
        characters = data.get("characters") or []
        if not isinstance(characters, list):
            raise ValueError("characters 必须是列表")
        cleaned = [item for item in characters if isinstance(item, dict) and str(item.get("name") or "").strip()]
        if not cleaned:
            raise ValueError("作品没有可用角色")
        data["characters"] = cleaned
        data["source"] = source
        return cls.model_validate(data)