"""Versioned knowledge / play-mode / model-preset API (/api/v1).

Authored configuration is created and validated here, never executed. Every
write is validated before it is stored, so a broken storyline can never become
the authoritative configuration of a playable scenario.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db.scenario_store import ScenarioStore
from app.models.knowledge import (
    LAYER_LABELS,
    LAYER_ORDER,
    KnowledgeContext,
    KnowledgeEntry,
    KnowledgeEntryCreate,
    KnowledgeEntryPatch,
    ModelPreset,
    ModelPresetCreate,
    ModelPresetPatch,
    PlayConfig,
    PlayConfigUpdate,
)
from app.services import knowledge_service

router = APIRouter(tags=["knowledge"])

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _store() -> ScenarioStore:
    path = os.environ.get("SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db"))
    return ScenarioStore(path)


class ValidateResponse(BaseModel):
    valid: bool
    errors: List[str] = Field(default_factory=list)


class LayersResponse(BaseModel):
    layers: List[Dict[str, str]] = Field(default_factory=list)
    modes: List[Dict[str, str]] = Field(default_factory=list)


def _require_scenario(store: ScenarioStore, scenario_id: str):
    scenario = store.get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"unknown scenario '{scenario_id}'")
    return scenario


# ------------------------------------------------------------------ metadata
@router.get("/knowledge/layers", response_model=LayersResponse)
def list_layers():
    return LayersResponse(
        layers=[{"id": layer, "label": LAYER_LABELS[layer]} for layer in LAYER_ORDER],
        modes=[
            {"id": "storyline", "label": "故事线模式（按主干线与分支发展）"},
            {"id": "free", "label": "自由模式（无预设故事线）"},
        ],
    )


# ------------------------------------------------------------ knowledge CRUD
@router.get(
    "/scenarios/{scenario_id}/knowledge", response_model=List[KnowledgeEntry]
)
def list_knowledge(scenario_id: str, include_disabled: bool = Query(True)):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        return store.list_knowledge(scenario_id, include_disabled=include_disabled)
    finally:
        store.close()


@router.post(
    "/scenarios/{scenario_id}/knowledge",
    response_model=KnowledgeEntry,
    status_code=201,
)
def create_knowledge(scenario_id: str, body: KnowledgeEntryCreate):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        return store.add_knowledge(scenario_id, body)
    finally:
        store.close()


@router.patch("/knowledge/{entry_id}", response_model=KnowledgeEntry)
def patch_knowledge(entry_id: str, body: KnowledgeEntryPatch):
    store = _store()
    try:
        entry = store.get_knowledge(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown knowledge '{entry_id}'")
        updated = store.update_knowledge(entry_id, body)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"unknown knowledge '{entry_id}'")
        return updated
    finally:
        store.close()


@router.delete("/knowledge/{entry_id}")
def delete_knowledge(entry_id: str):
    store = _store()
    try:
        if not store.delete_knowledge(entry_id):
            raise HTTPException(status_code=404, detail=f"unknown knowledge '{entry_id}'")
        return {"deleted": True, "id": entry_id}
    finally:
        store.close()


# --------------------------------------------------------------- play config
@router.get("/scenarios/{scenario_id}/play-config", response_model=PlayConfig)
def get_play_config(scenario_id: str):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        return store.get_play_config(scenario_id)
    finally:
        store.close()


@router.put("/scenarios/{scenario_id}/play-config", response_model=PlayConfig)
def put_play_config(scenario_id: str, body: PlayConfigUpdate):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        current = store.get_play_config(scenario_id)
        config = PlayConfig(
            scenario_id=scenario_id,
            mode=body.mode,
            storyline=body.storyline if body.storyline is not None else current.storyline,
            max_depth=body.max_depth if body.max_depth is not None else current.max_depth,
            model_preset_id=(
                body.model_preset_id
                if body.model_preset_id is not None
                else current.model_preset_id
            ),
        )
        if config.model_preset_id and store.get_model_preset(config.model_preset_id) is None:
            raise HTTPException(
                status_code=400,
                detail=[f"unknown model preset '{config.model_preset_id}'"],
            )
        errors = knowledge_service.validate_play_config(
            config, store.list_knowledge(scenario_id)
        )
        if errors:
            raise HTTPException(status_code=400, detail=errors)
        return store.set_play_config(scenario_id, config)
    finally:
        store.close()


@router.post(
    "/scenarios/{scenario_id}/knowledge/validate", response_model=ValidateResponse
)
def validate_knowledge(scenario_id: str):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        config = store.get_play_config(scenario_id)
        errors = knowledge_service.validate_play_config(
            config, store.list_knowledge(scenario_id)
        )
        return ValidateResponse(valid=not errors, errors=errors)
    finally:
        store.close()


@router.get(
    "/scenarios/{scenario_id}/context/preview", response_model=KnowledgeContext
)
def preview_context(
    scenario_id: str,
    mode: Optional[str] = Query(None, pattern="^(free|storyline)$"),
    max_depth: Optional[int] = Query(None, ge=0, le=9),
    story_node: Optional[str] = Query(None),
):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        config = store.get_play_config(scenario_id)
        entries = store.list_knowledge(scenario_id)
        return knowledge_service.build_context(
            entries,
            mode=mode or config.mode,
            max_depth=max_depth if max_depth is not None else config.max_depth,
            story_node=story_node,
            storyline=config.storyline,
            scenario_id=scenario_id,
        )
    finally:
        store.close()


# ------------------------------------------------------------- model presets
@router.get("/model-presets", response_model=List[ModelPreset])
def list_presets():
    store = _store()
    try:
        return store.list_model_presets()
    finally:
        store.close()


@router.post("/model-presets", response_model=ModelPreset, status_code=201)
def create_preset(body: ModelPresetCreate):
    store = _store()
    try:
        return store.add_model_preset(body)
    finally:
        store.close()


@router.patch("/model-presets/{preset_id}", response_model=ModelPreset)
def patch_preset(preset_id: str, body: ModelPresetPatch):
    store = _store()
    try:
        preset = store.get_model_preset(preset_id)
        if preset is None:
            raise HTTPException(status_code=404, detail=f"unknown preset '{preset_id}'")
        updated = store.update_model_preset(preset_id, body)
        if updated is None:
            raise HTTPException(status_code=404, detail=f"unknown preset '{preset_id}'")
        return updated
    finally:
        store.close()


@router.delete("/model-presets/{preset_id}")
def remove_preset(preset_id: str):
    store = _store()
    try:
        preset = store.get_model_preset(preset_id)
        if preset is None:
            raise HTTPException(status_code=404, detail=f"unknown preset '{preset_id}'")
        if preset.is_builtin:
            raise HTTPException(
                status_code=409, detail="builtin presets cannot be deleted; edit them instead"
            )
        store.delete_model_preset(preset_id)
        return {"deleted": True, "id": preset_id}
    finally:
        store.close()
