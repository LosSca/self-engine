"""Character pack API: the character library and its appearance variants.

A character is configuration (name, role, description, portrait). Each variant
("look") pairs an outfit with an expression and one image, so the UI can show a
portrait that matches the current costume and mood. Campaigns store only the
player's chosen ids, so game state is never copied into art metadata.

Characters are authored once in a global library (``/characters``) and can be
brought into any world: a campaign binds whatever character the player picked,
regardless of which scenario it was authored under. The per-scenario route
(``/scenarios/{id}/characters``) still exists and returns that scenario own
characters plus the shared library, so older clients keep working.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.config import settings
from app.db.scenario_store import ScenarioCharacter, ScenarioStore

router = APIRouter()

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAX_UPLOAD_BYTES = 6 * 1024 * 1024
_ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _get_store() -> ScenarioStore:
    db_path = os.environ.get("SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db"))
    return ScenarioStore(db_path)


def _media_root() -> Path:
    return Path(settings.media_dir).expanduser().resolve()


def _look_dict(look) -> dict:
    return {
        "id": look.id,
        "character_id": look.character_id,
        "outfit": look.outfit,
        "expression": look.expression,
        "image_url": look.image_url,
        "sort_order": look.sort_order,
    }


def _character_dict(character: ScenarioCharacter) -> dict:
    return {
        "id": character.id,
        "scenario_id": character.scenario_id,
        "name": character.name,
        "role": character.role,
        "description": character.description,
        "portrait_url": character.portrait_url,
        "is_player": character.is_player,
        "created_at": character.created_at,
        "looks": [_look_dict(look) for look in character.looks],
    }


class CharacterCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    role: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=4000)
    portrait_url: str = Field(default="", max_length=1000)
    is_player: bool = False


class CharacterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    role: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    portrait_url: str | None = Field(default=None, max_length=1000)
    is_player: bool | None = None


class LookCreate(BaseModel):
    outfit: str = Field(default="默认", max_length=60)
    expression: str = Field(default="平静", max_length=60)
    image_url: str = Field(default="", max_length=1000)
    sort_order: int | None = None


class LookUpdate(BaseModel):
    outfit: str | None = Field(default=None, max_length=60)
    expression: str | None = Field(default=None, max_length=60)
    image_url: str | None = Field(default=None, max_length=1000)
    sort_order: int | None = None


class CampaignCharacterUpdate(BaseModel):
    character_id: str = Field(default="", max_length=64)
    look_id: str = Field(default="", max_length=64)
    outfit: str = Field(default="默认", max_length=60)
    expression: str = Field(default="平静", max_length=60)
    name: str = Field(default="", max_length=80)


@router.get("/scenarios/{scenario_id}/characters")
def list_characters(scenario_id: str):
    with _get_store() as store:
        if not store.get_scenario(scenario_id):
            raise HTTPException(status_code=404, detail="Scenario not found")
        return [_character_dict(c) for c in store.list_characters(scenario_id)]


@router.post("/scenarios/{scenario_id}/characters", status_code=201)
def create_character(scenario_id: str, req: CharacterCreate):
    with _get_store() as store:
        if not store.get_scenario(scenario_id):
            raise HTTPException(status_code=404, detail="Scenario not found")
        character = store.add_character(
            scenario_id,
            name=req.name.strip(),
            role=req.role.strip(),
            description=req.description,
            portrait_url=req.portrait_url,
            is_player=req.is_player,
        )
        return _character_dict(character)


# -- character library -------------------------------------------------
# A character is no longer owned by one scenario: it is authored once and
# can be brought into any world. These routes are the library surface.


@router.get("/characters")
def list_library_characters():
    """Every character authored outside a scenario (the shared library)."""
    with _get_store() as store:
        return [_character_dict(c) for c in store.list_library_characters()]


@router.post("/characters", status_code=201)
def create_library_character(req: CharacterCreate):
    """Create a character that belongs to no scenario."""
    with _get_store() as store:
        character = store.add_character(
            "",
            name=req.name.strip(),
            role=req.role.strip(),
            description=req.description,
            portrait_url=req.portrait_url,
            is_player=req.is_player,
        )
        return _character_dict(character)


@router.post("/characters/{character_id}/library/import", status_code=201)
def import_character_to_library(character_id: str):
    """Copy a scenario-owned character into the library, keeping its looks.

    The original row is left untouched so existing campaigns that reference
    it keep resolving; the copy is the one that becomes world-independent.
    """
    with _get_store() as store:
        source = store.get_character(character_id)
        if not source:
            raise HTTPException(status_code=404, detail="Character not found")
        if not source.scenario_id:
            return _character_dict(source)
        clone = store.add_character(
            "",
            name=source.name,
            role=source.role,
            description=source.description,
            portrait_url=source.portrait_url,
            is_player=source.is_player,
        )
        for look in source.looks:
            store.add_look(clone.id, look.outfit, look.expression, look.image_url)
        return _character_dict(store.get_character(clone.id) or clone)


@router.get("/characters/{character_id}")
def get_character(character_id: str):
    with _get_store() as store:
        character = store.get_character(character_id)
        if not character:
            raise HTTPException(status_code=404, detail="Character not found")
        return _character_dict(character)


@router.patch("/characters/{character_id}")
def update_character(character_id: str, req: CharacterUpdate):
    with _get_store() as store:
        if not store.get_character(character_id):
            raise HTTPException(status_code=404, detail="Character not found")
        store.update_character(character_id, **req.model_dump(exclude_unset=True))
        character = store.get_character(character_id)
        return _character_dict(character)


@router.delete("/characters/{character_id}")
def delete_character(character_id: str):
    with _get_store() as store:
        if not store.delete_character(character_id):
            raise HTTPException(status_code=404, detail="Character not found")
        return {"deleted": True}


@router.post("/characters/{character_id}/looks", status_code=201)
def create_look(character_id: str, req: LookCreate):
    with _get_store() as store:
        if not store.get_character(character_id):
            raise HTTPException(status_code=404, detail="Character not found")
        if store.get_character(character_id).look_for(req.outfit, req.expression):
            raise HTTPException(
                status_code=409,
                detail=f"look '{req.outfit} / {req.expression}' already exists",
            )
        look = store.add_look(
            character_id, outfit=req.outfit, expression=req.expression,
            image_url=req.image_url, sort_order=req.sort_order,
        )
        return _look_dict(look)


@router.patch("/characters/looks/{look_id}")
def update_look(look_id: str, req: LookUpdate):
    with _get_store() as store:
        if not store.get_look(look_id):
            raise HTTPException(status_code=404, detail="Look not found")
        store.update_look(look_id, **req.model_dump(exclude_unset=True))
        return _look_dict(store.get_look(look_id))


@router.delete("/characters/looks/{look_id}")
def delete_look(look_id: str):
    with _get_store() as store:
        if not store.delete_look(look_id):
            raise HTTPException(status_code=404, detail="Look not found")
        return {"deleted": True}


@router.get("/campaigns/{campaign_id}/character")
def get_campaign_character(campaign_id: str):
    with _get_store() as store:
        campaign = store.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        picks = campaign.character_picks or {}
        character = store.get_character(picks.get("character_id", "")) if picks.get("character_id") else None
        look = store.get_look(picks.get("look_id", "")) if picks.get("look_id") else None
        if character and not look:
            look = character.look_for(picks.get("outfit", ""), picks.get("expression", ""))
        return {
            "campaign_id": campaign_id,
            "picks": picks,
            "character": _character_dict(character) if character else None,
            "look": _look_dict(look) if look else None,
        }


@router.put("/campaigns/{campaign_id}/character")
def set_campaign_character(campaign_id: str, req: CampaignCharacterUpdate):
    with _get_store() as store:
        campaign = store.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        character = store.get_character(req.character_id) if req.character_id else None
        if req.character_id and not character:
            raise HTTPException(status_code=404, detail="Character not found")
        # A character is no longer tied to the scenario it was authored under:
        # the player may bring any character into any world. Only a missing
        # character is an error.
        look = store.get_look(req.look_id) if req.look_id else None
        if req.look_id and not look:
            raise HTTPException(status_code=404, detail="Look not found")
        if look and character and look.character_id != character.id:
            raise HTTPException(status_code=409, detail="Look belongs to another character")
        picks = {
            "character_id": req.character_id,
            "look_id": req.look_id,
            "outfit": (look.outfit if look else req.outfit),
            "expression": (look.expression if look else req.expression),
            "name": req.name or (character.name if character else ""),
        }
        store.update_campaign_character(campaign_id, picks)
        return {"campaign_id": campaign_id, "picks": picks}


@router.post("/characters/upload")
async def upload_character_image(file: UploadFile = File(...)):
    """Store one character image under the served /media directory."""
    ext = _ALLOWED_IMAGE_TYPES.get((file.content_type or "").lower())
    if not ext:
        raise HTTPException(
            status_code=400,
            detail="Unsupported image type; use PNG, JPEG, WebP or GIF",
        )
    payload = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(payload) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Image is larger than 6 MB")
    if not payload:
        raise HTTPException(status_code=400, detail="Empty upload")
    target_dir = _media_root() / "characters"
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{ext}"
    (target_dir / filename).write_bytes(payload)
    return {"url": f"/media/characters/{filename}", "bytes": len(payload)}
