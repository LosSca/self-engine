"""Library API: works, their pickable characters, and cross-over entry.

A work profile is *advisory*: it feeds prompts and pickers.  Entering a work
does two concrete, stored things and nothing else -- it creates a world
skeleton from the profile and, when the campaign is known, records which
character the player borrowed.  Inventory, doors and quest progress still only
move through the action pipeline.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.models.work import WorkProfile
from app.services import ai_generator, work_catalogue, world_service

router = APIRouter()

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _scenario_store():
    from app.db.scenario_store import ScenarioStore

    db_path = os.environ.get("SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db"))
    return ScenarioStore(db_path)


def _game_store():
    from app.db.game_state_store import GameStateStore

    db_path = os.environ.get("GAME_STATE_DB_PATH", os.path.join(_BACKEND_DIR, "game_state.db"))
    return GameStateStore(db_path)


class WorkCharacterView(BaseModel):
    name: str
    role: str = ""
    description: str = ""
    traits: List[str] = Field(default_factory=list)
    source_work: str = ""


class WorkProfileView(BaseModel):
    title: str
    medium: str = ""
    summary: str = ""
    tone: str = ""
    locations: List[str] = Field(default_factory=list)
    characters: List[WorkCharacterView] = Field(default_factory=list)
    opening_hint: str = ""
    source: str = "curated"


class ResolveWorkRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    preset_id: str = ""
    language: str = "中文"
    #: When False, a catalogue miss is a 404 instead of a model call.  The UI
    #: uses this for its confirm step; a typo should not cost a request.
    allow_model: bool = True


class EnterWorkRequest(BaseModel):
    work_title: str = Field(..., min_length=1, max_length=200)
    character_name: str = Field(..., min_length=1, max_length=80)
    scenario_id: str = Field("", max_length=64)
    campaign_id: str = Field("", max_length=64)
    world_id: str = Field("", max_length=64)
    preset_id: str = ""
    language: str = "中文"
    allow_model: bool = True


class EnterWorkResponse(BaseModel):
    status: str
    work: str
    character: WorkCharacterView
    world_id: str
    world_name: str
    locations: int
    scenario_id: str = ""
    campaign_id: str = ""
    character_pick: Optional[Dict[str, Any]] = None


def _view(profile: WorkProfile) -> WorkProfileView:
    return WorkProfileView(
        title=profile.title,
        medium=profile.medium,
        summary=profile.summary,
        tone=profile.tone,
        locations=list(profile.locations),
        characters=[WorkCharacterView(**row) for row in work_catalogue.character_catalogue(profile)],
        opening_hint=profile.opening_hint,
        source=profile.source,
    )


def _preset_or_400(preset_id: str):
    if not preset_id:
        return None
    store = _scenario_store()
    try:
        preset = store.get_model_preset(preset_id)
    finally:
        store.close()
    if preset is None:
        raise HTTPException(status_code=400, detail=f"unknown model preset '{preset_id}'")
    return preset


async def _generate(title: str, preset_id: str, language: str) -> WorkProfile:
    from app.services.work_generator import generate_work

    preset = _preset_or_400(preset_id)
    model_router = ai_generator.router_for(preset)
    if model_router is None:
        raise HTTPException(status_code=503, detail="未配置可用模型：请先设置供应商密钥或本地模型")
    try:
        return await generate_work(model_router, title, language)
    except (ValueError, ai_generator.GenerationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/works", response_model=List[WorkProfileView])
def list_works():
    """The curated catalogue.  Read-only; nothing here calls a model."""
    return [_view(work_catalogue.find_work(key)) for key in work_catalogue.catalogue_keys()]


@router.get("/works/{title}", response_model=WorkProfileView)
def get_work(title: str):
    """Curated lookup only -- an unknown title is a 404, never a model call."""
    profile = work_catalogue.find_work(title)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown work '{title}'")
    return _view(profile)


@router.post("/works/resolve", response_model=WorkProfileView)
async def resolve_work(body: ResolveWorkRequest):
    """Curated first; the model is an explicit opt-in fallback."""
    local = work_catalogue.find_work(body.title)
    if local is not None:
        return _view(local)
    if not body.allow_model:
        raise HTTPException(
            status_code=404,
            detail=f"作品《{body.title}》不在本地档案里；确认生成后再试",
        )
    return _view(await _generate(body.title, body.preset_id, body.language))


@router.post("/works/enter", response_model=EnterWorkResponse)
async def enter_work(body: EnterWorkRequest):
    """Cross over: build the world skeleton and, optionally, record the role."""
    profile = work_catalogue.find_work(body.work_title)
    if profile is None:
        if not body.allow_model:
            raise HTTPException(status_code=404, detail=f"unknown work '{body.work_title}'")
        profile = await _generate(body.work_title, body.preset_id, body.language)

    character = next((row for row in profile.characters if row.name == body.character_name), None)
    if character is None:
        names = ", ".join(row.name for row in profile.characters)
        raise HTTPException(
            status_code=400,
            detail=f"《{profile.title}》里没有角色 '{body.character_name}'；可选：{names}",
        )

    scenario_store = _scenario_store()
    try:
        if body.scenario_id and scenario_store.get_scenario(body.scenario_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown scenario '{body.scenario_id}'")
        scenario = scenario_store.get_scenario(body.scenario_id) if body.scenario_id else None
    finally:
        scenario_store.close()

    world_id = body.world_id.strip() or f"crossover_{work_catalogue.key_for(profile.title)}"
    world = work_catalogue.seed_world_definition(profile, character, world_id, body.scenario_id)
    game_store = _game_store()
    try:
        summary = world_service.save_world(game_store, world)
    except world_service.WorldValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors) from exc
    finally:
        game_store.close()

    pick = None
    if body.campaign_id:
        scenario_store = _scenario_store()
        try:
            campaign = scenario_store.get_campaign(body.campaign_id)
            if campaign is None:
                raise HTTPException(status_code=404, detail=f"unknown campaign '{body.campaign_id}'")
            pick = {
                "character_id": "",
                "look_id": "",
                "outfit": "默认",
                "expression": "平静",
                "name": character.name,
                "source_work": profile.title,
                "role": character.role,
            }
            scenario_store.update_campaign_character(body.campaign_id, pick)
        finally:
            scenario_store.close()

    return EnterWorkResponse(
        status="saved",
        work=profile.title,
        character=WorkCharacterView(**work_catalogue.character_catalogue(profile)[[c.name for c in profile.characters].index(character.name)]),
        world_id=summary["world_id"],
        world_name=summary["name"],
        locations=summary["locations"],
        scenario_id=body.scenario_id,
        campaign_id=body.campaign_id,
        character_pick=pick,
    )