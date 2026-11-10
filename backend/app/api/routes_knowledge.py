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
from app.engines import story_engine
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
from app.services import ai_generator, knowledge_service

router = APIRouter(tags=["knowledge"])

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _store() -> ScenarioStore:
    path = os.environ.get("SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db"))
    return ScenarioStore(path)


def _scenario_event_ids(scenario_id: str) -> Optional[set[str]]:
    """World event ids available to this scenario, if a world is bound.

    Storyline saves without a world keep the previous behaviour (no event
    validation). Once a world exists, a stale requires_event is an authoring
    error rather than a node that silently never fires.
    """
    from app.db.game_state_store import GameStateStore

    path = os.environ.get(
        "GAME_STATE_DB_PATH", os.path.join(_BACKEND_DIR, "game_state.db")
    )
    if not os.path.exists(path):
        return None
    store = GameStateStore(path)
    try:
        worlds = [
            item for item in store.list_worlds()
            if item.get("scenario_id") == scenario_id
        ]
        if not worlds:
            return None
        known: set[str] = set()
        for item in worlds:
            world = store.get_world(item["world_id"])
            if world is not None:
                known.update(event.id for event in world.events)
        return known
    finally:
        store.close()


class ValidateResponse(BaseModel):
    valid: bool
    errors: List[str] = Field(default_factory=list)


class GenerateKnowledgeRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    count: int = Field(6, ge=1, le=20)
    preset_id: str = ""
    language: str = "中文"


class GenerateStorylineRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    node_count: int = Field(6, ge=2, le=20)
    preset_id: str = ""
    language: str = "中文"


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
            context_budget_chars=(
                body.context_budget_chars
                if body.context_budget_chars is not None
                else current.context_budget_chars
            ),
            model_preset_id=(
                body.model_preset_id
                if body.model_preset_id is not None
                else current.model_preset_id
            ),
            intent_preset_id=(
                body.intent_preset_id
                if body.intent_preset_id is not None
                else current.intent_preset_id
            ),
            narrator_prompt=(
                body.narrator_prompt
                if body.narrator_prompt is not None
                else current.narrator_prompt
            ),
            dialogue_preset_id=(
                body.dialogue_preset_id
                if body.dialogue_preset_id is not None
                else current.dialogue_preset_id
            ),
            summary_preset_id=(
                body.summary_preset_id
                if body.summary_preset_id is not None
                else current.summary_preset_id
            ),
        )
        for preset_id in (
            config.model_preset_id,
            config.intent_preset_id,
            config.dialogue_preset_id,
            config.summary_preset_id,
        ):
            if preset_id and store.get_model_preset(preset_id) is None:
                raise HTTPException(
                    status_code=400,
                    detail=[f"unknown model preset '{preset_id}'"],
                )
        errors = knowledge_service.validate_play_config(
            config,
            store.list_knowledge(scenario_id),
            event_ids=_scenario_event_ids(scenario_id),
        )
        # Settled (locked) content is read-only for the API as well, so a buggy
        # or scripted client cannot rewrite a story that is already running.
        errors = errors + story_engine.protected_edits(
            current.storyline, config.storyline
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
            config,
            store.list_knowledge(scenario_id),
            event_ids=_scenario_event_ids(scenario_id),
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
    story_nodes: Optional[str] = Query(
        None, description="逗号分隔的已进入节点 id；提供时按它们的并集提升知识"
    ),
    focus: Optional[str] = Query(
        None, description="逗号分隔的聚焦词（当前位置、目标、NPC），命中的标签优先"
    ),
    query: Optional[str] = Query(
        None, description="用于稀疏向量相似度的查询文本（通常是玩家这句话）"
    ),
    campaign_id: Optional[str] = Query(
        None, description="提供战役 id 时按该战役的权威状态计算已启用的常驻事件包"
    ),
):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        config = store.get_play_config(scenario_id)
        entries = store.list_knowledge(scenario_id)
        entered = [part.strip() for part in (story_nodes or "").split(",") if part.strip()]
        focused = [part.strip() for part in (focus or "").split(",") if part.strip()]
        armed = _armed_packs(config.storyline, campaign_id)
        return knowledge_service.build_context(
            entries,
            mode=mode or config.mode,
            max_depth=max_depth if max_depth is not None else config.max_depth,
            story_node=story_node,
            storyline=config.storyline,
            scenario_id=scenario_id,
            story_nodes=entered or None,
            focus=focused or None,
            budget_chars=config.context_budget_chars,
            query=query or "",
            armed_packs=armed or None,
        )
    finally:
        store.close()


# ------------------------------------------------------- AI drafting
def _armed_packs(storyline, campaign_id: Optional[str]) -> list:
    """Armed event packs for one campaign, or [] when it cannot be known."""
    if not campaign_id or not storyline.packs:
        return []
    from app.api.routes_engine import _get_store
    from app.services import world_service

    game_store = _get_store()
    try:
        state = game_store.get_state(campaign_id)
        if state is None:
            return []
        try:
            world = world_service.get_world(game_store, state.world_id)
        except LookupError:
            return []
        return knowledge_service.armed_pack_ids(storyline, state, world)
    finally:
        game_store.close()


def _generation_router(store: ScenarioStore, preset_id: str):
    preset = store.get_model_preset(preset_id) if preset_id else None
    if preset_id and preset is None:
        raise HTTPException(status_code=400, detail=[f"unknown model preset '{preset_id}'"])
    return ai_generator.router_for(preset)


@router.post("/scenarios/{scenario_id}/generate/knowledge")
async def generate_knowledge(
    scenario_id: str, body: GenerateKnowledgeRequest
):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        router = _generation_router(store, body.preset_id)
        if router is None:
            raise HTTPException(
                status_code=503,
                detail="未配置可用模型：请先设置供应商密钥或本地模型",
            )
        try:
            drafts = await ai_generator.generate_knowledge(
                router, body.prompt, body.count, body.language
            )
        except ai_generator.GenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return {"entries": drafts, "valid": len([d for d in drafts if "_error" not in d])}
    finally:
        store.close()


@router.post("/scenarios/{scenario_id}/generate/storyline")
async def generate_storyline(
    scenario_id: str, body: GenerateStorylineRequest
):
    store = _store()
    try:
        _require_scenario(store, scenario_id)
        router = _generation_router(store, body.preset_id)
        if router is None:
            raise HTTPException(
                status_code=503,
                detail="未配置可用模型：请先设置供应商密钥或本地模型",
            )
        knowledge_ids = [entry.id for entry in store.list_knowledge(scenario_id)]
        event_ids = _scenario_event_ids(scenario_id)
        try:
            draft = await ai_generator.generate_storyline(
                router,
                body.prompt,
                body.node_count,
                knowledge_ids=knowledge_ids,
                event_ids=sorted(event_ids) if event_ids is not None else None,
                language=body.language,
            )
        except ai_generator.GenerationError as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        return draft
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
