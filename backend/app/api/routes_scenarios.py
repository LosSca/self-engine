import logging
import os
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.db.scenario_store import ScenarioStore, StoryCardType
from app.db.event_store import EventStore
from app.models.knowledge import (
    KnowledgeEntry,
    KnowledgeEntryCreate,
    PlayConfig,
    Storyline,
    default_storyline,
)
from app.services.knowledge_service import validate_play_config
from app.engines.llm_router import LLMRouter, LLMConfig
from app.engines.opening_generator import (
    format_setup_lines,
    generate_opening,
    synthesize_sample_answers,
)
from app.services.draft_generator import DRAFT_KINDS, expand_draft, generate_draft

logger = logging.getLogger(__name__)

router = APIRouter()


_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _get_store() -> ScenarioStore:
    db_path = os.environ.get("SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db"))
    return ScenarioStore(db_path)


def _get_event_store() -> EventStore:
    db_path = os.environ.get("EVENT_DB_PATH", os.path.join(_BACKEND_DIR, "events.db"))
    return EventStore(db_path)


class SetupOption(BaseModel):
    label: str = Field(..., max_length=200)
    description: str = Field(default="", max_length=4000)


class SetupQuestion(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    var_name: str = Field(..., min_length=1, max_length=64)
    prompt: str = Field(..., min_length=1, max_length=4000)
    type: str = Field(..., pattern=r"^(text|choice)$")
    options: list[SetupOption] = []
    allow_custom: bool = False
    required: bool = True


class CreateScenarioRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    tone_instructions: str = Field(default="", max_length=50000)
    opening_narrative: str = Field(default="", max_length=50000)
    language: str = Field(default="zh", max_length=10)
    lore_text: str = Field(default="", max_length=50000)
    setup_questions: list[SetupQuestion] = []
    opening_mode: Literal["fixed", "ai"] = "fixed"
    ai_opening_directive: str = Field(default="", max_length=4000)
    world_type: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def _opening_required(self) -> "CreateScenarioRequest":
        """Every world needs an opening scene.

        A world with no opening leaves the player staring at an empty screen
        with no premise, so either a written opening or an AI directive to
        generate one is mandatory.
        """
        if not self.opening_narrative.strip() and not self.ai_opening_directive.strip():
            raise ValueError("每个世界都需要开场白：请写下开场，或让 AI 按指令生成")
        return self


class PreviewOpeningRequest(BaseModel):
    language: str = Field(default="en", max_length=10)
    tone: str = Field(default="", max_length=50000)
    lore: str = Field(default="", max_length=50000)
    directive: str = Field(default="", max_length=4000)
    setup_questions: list[SetupQuestion] = []
    sample_answers: dict = Field(default_factory=dict)


class DraftRequest(BaseModel):
    """One "write me a first draft" call from the authoring UIs."""

    kind: str = Field(..., max_length=20)
    topic: str = Field(default="", max_length=200)
    context: str = Field(default="", max_length=8000)
    language: str = Field(default="zh", max_length=10)
    #: Ground the draft in a web lookup of the topic (real people, myths, worlds).
    search: bool = False


class ExpandRequest(BaseModel):
    """Continue what the author already wrote instead of replacing it."""

    kind: str = Field(..., max_length=20)
    text: str = Field(..., max_length=8000)
    topic: str = Field(default="", max_length=200)
    language: str = Field(default="zh", max_length=10)
    search: bool = False


class SearchRequest(BaseModel):
    """Preview the web hits a grounded draft would use."""

    query: str = Field(..., max_length=200)
    limit: int = Field(default=5, ge=1, le=10)


class AddStoryCardRequest(BaseModel):
    card_type: StoryCardType
    name: str = Field(..., min_length=1, max_length=200)
    content: dict = {}


class CampaignData(BaseModel):
    player_name: str


class CharacterLookData(BaseModel):
    outfit: str = "默认"
    expression: str = "平静"
    image_url: str = ""


class CharacterData(BaseModel):
    name: str
    role: str = ""
    description: str = ""
    portrait_url: str = ""
    is_player: bool = False
    looks: list[CharacterLookData] = []


class KnowledgeData(KnowledgeEntryCreate):
    """One authored knowledge entry, with its export-time id for remapping."""

    id: str = ""


class PlayConfigData(BaseModel):
    model_config = {"protected_namespaces": ()}

    mode: Literal["storyline", "free"] = "free"
    storyline: Storyline = Storyline()
    max_depth: int = Field(9, ge=0, le=9)
    model_preset_id: str = ""


class ImportScenarioRequest(BaseModel):
    version: str
    scenario: CreateScenarioRequest
    story_cards: list[AddStoryCardRequest] = []
    campaigns: list[CampaignData] = []
    characters: list[CharacterData] = []
    knowledge: list[KnowledgeData] = []
    play_config: PlayConfigData | None = None


def _validate_unique_var_names(questions: list[SetupQuestion]) -> None:
    seen: set[str] = set()
    for q in questions:
        if q.var_name in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate var_name: {q.var_name}")
        seen.add(q.var_name)


@router.post("/", status_code=201)
def create_scenario(req: CreateScenarioRequest):
    _validate_unique_var_names(req.setup_questions)
    with _get_store() as store:
        scenario = store.create_scenario(
            title=req.title,
            description=req.description,
            tone_instructions=req.tone_instructions,
            opening_narrative=req.opening_narrative,
            language=req.language,
            lore_text=req.lore_text,
            setup_questions=[q.model_dump() for q in req.setup_questions],
            opening_mode=req.opening_mode,
            ai_opening_directive=req.ai_opening_directive,
            world_type=req.world_type,
        )
        # New scenarios open in storyline mode with a two-node starter graph, so
        # an author can press play immediately and still sees where to hook the
        # first real branch. It is ordinary editable data, not a special case.
        store.set_play_config(
            scenario.id,
            PlayConfig(
                scenario_id=scenario.id,
                mode="storyline",
                storyline=default_storyline(req.language),
                max_depth=9,
            ),
        )
    return scenario.__dict__


@router.post("/draft")
async def draft_text(req: DraftRequest):
    """Draft one field for the character library or the scenario builder.

    Lives under /scenarios because both authoring screens need it and neither
    should carry its own prompt. With no model configured this still answers
    with a template draft, so the button is never dead.
    """
    if req.kind not in DRAFT_KINDS:
        raise HTTPException(status_code=400, detail=f"unsupported kind: {req.kind}")
    text, provider = await generate_draft(
        req.kind,
        topic=req.topic,
        context=req.context,
        language=req.language,
        search=req.search,
    )
    return {"text": text, "provider": provider}


@router.post("/draft/expand")
async def expand_text(req: ExpandRequest):
    """Continue an existing draft; the UI appends the answer to the field."""
    if req.kind not in DRAFT_KINDS:
        raise HTTPException(status_code=400, detail=f"unsupported kind: {req.kind}")
    text, provider = await expand_draft(
        req.kind,
        text=req.text,
        topic=req.topic,
        language=req.language,
        search=req.search,
    )
    return {"text": text, "provider": provider}


@router.post("/draft/search")
async def draft_search(req: SearchRequest):
    """The web hits a grounded draft would be built from.

    Separate from /draft so the UI can show what the model is about to read and
    let the author keep or drop the lookup before spending a generation.
    """
    from app.services.web_search import web_search

    hits = await web_search(req.query, limit=req.limit)
    return {"results": hits, "count": len(hits)}


@router.post("/preview-opening")
async def preview_opening(req: PreviewOpeningRequest):
    """Render a sample AI opening for the scenario builder.

    Uses ``sample_answers`` if supplied, otherwise synthesizes them from the
    scenario's setup questions so authors get a usable preview without having
    to fill in mock data by hand.
    """
    questions = [q.model_dump() for q in req.setup_questions]
    answers = req.sample_answers or synthesize_sample_answers(questions)
    lines = format_setup_lines(answers, questions)
    router_ = LLMRouter(LLMConfig())
    text = await generate_opening(
        language=req.language,
        tone=req.tone,
        lore=req.lore,
        character_setup_lines=lines,
        director_note=req.directive,
        router=router_,
    )
    return {"opening": text, "sample_answers": answers}


@router.get("/")
def list_scenarios():
    with _get_store() as store:
        return [s.__dict__ for s in store.list_scenarios()]


@router.post("/import", status_code=201)
def import_scenario(req: ImportScenarioRequest):
    # 导入同样要开场白：老导出文件里可能没有，给出可执行的提示而不是静默建出空世界。
    if not req.scenario.opening_narrative.strip() and not req.scenario.ai_opening_directive.strip():
        raise HTTPException(
            status_code=400,
            detail="这个世界没有开场白：请先在导入文件里补 opening_narrative 或 ai_opening_directive",
        )
    _validate_unique_var_names(req.scenario.setup_questions)
    with _get_store() as store:
        scenario = store.create_scenario(
            title=req.scenario.title,
            description=req.scenario.description,
            tone_instructions=req.scenario.tone_instructions,
            opening_narrative=req.scenario.opening_narrative,
            language=req.scenario.language,
            lore_text=req.scenario.lore_text,
            setup_questions=[q.model_dump() for q in req.scenario.setup_questions],
            opening_mode=req.scenario.opening_mode,
            ai_opening_directive=req.scenario.ai_opening_directive,
            world_type=req.scenario.world_type,
        )
        for data in req.characters:
            character = store.add_character(
                scenario.id, name=data.name, role=data.role,
                description=data.description, portrait_url=data.portrait_url,
                is_player=data.is_player,
            )
            for look in data.looks:
                store.add_look(
                    character.id, outfit=look.outfit,
                    expression=look.expression, image_url=look.image_url,
                )
        for card in req.story_cards:
            store.add_story_card(scenario.id, card.card_type, card.name, card.content)
        for campaign in req.campaigns:
            store.create_campaign(scenario.id, campaign.player_name)

        # Authored knowledge first, so storyline nodes can be remapped onto the
        # ids this install actually generated (export ids are per-install).
        id_map: dict = {}
        entries = []
        for data in req.knowledge:
            created = store.add_knowledge(
                scenario.id,
                KnowledgeEntryCreate(**data.model_dump(exclude={"id"})),
            )
            entries.append(created)
            if data.id:
                id_map[data.id] = created.id

        if req.play_config is not None:
            storyline = req.play_config.storyline.model_copy(deep=True)
            for node in storyline.nodes:
                node.knowledge_ids = [
                    id_map[kid] for kid in node.knowledge_ids if kid in id_map
                ]
            config = PlayConfig(
                scenario_id=scenario.id,
                mode=req.play_config.mode,
                storyline=storyline,
                max_depth=req.play_config.max_depth,
                model_preset_id=(
                    req.play_config.model_preset_id
                    if req.play_config.model_preset_id
                    and store.get_model_preset(req.play_config.model_preset_id)
                    else ""
                ),
            )
            errors = validate_play_config(config, entries)
            if errors:
                store.delete_scenario(scenario.id)
                raise HTTPException(status_code=400, detail=errors)
            store.set_play_config(scenario.id, config)
    return scenario.__dict__


@router.get("/{scenario_id}")
def get_scenario(scenario_id: str):
    with _get_store() as store:
        scenario = store.get_scenario(scenario_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return scenario.__dict__


@router.post("/{scenario_id}/story-cards", status_code=201)
def add_story_card(scenario_id: str, req: AddStoryCardRequest):
    with _get_store() as store:
        card = store.add_story_card(scenario_id, req.card_type, req.name, req.content)
    return card.__dict__


@router.get("/{scenario_id}/story-cards")
def get_story_cards(scenario_id: str):
    with _get_store() as store:
        return [c.__dict__ for c in store.get_story_cards(scenario_id)]


@router.post("/{scenario_id}/campaigns", status_code=201)
def create_campaign(scenario_id: str, req: CampaignData):
    with _get_store() as store:
        scenario = store.get_scenario(scenario_id)
        if not scenario:
            raise HTTPException(status_code=404, detail="Scenario not found")
        campaign = store.create_campaign(scenario_id, req.player_name)
    return campaign.__dict__


@router.get("/{scenario_id}/campaigns")
def get_campaigns(scenario_id: str):
    with _get_store() as store:
        scenario = store.get_scenario(scenario_id)
        campaigns = store.get_campaigns(scenario_id) if scenario else None
    if scenario is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return [c.__dict__ for c in campaigns]


@router.get("/{scenario_id}/export")
def export_scenario(scenario_id: str):
    with _get_store() as store:
        scenario = store.get_scenario(scenario_id)
        story_cards = store.get_story_cards(scenario_id) if scenario else []
        campaigns = store.get_campaigns(scenario_id) if scenario else []
        characters = store.list_characters(scenario_id) if scenario else []
    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    knowledge: list[KnowledgeEntry] = []
    play_config: PlayConfig | None = None
    if scenario:
        with _get_store() as store:
            knowledge = store.list_knowledge(scenario_id)
            play_config = store.get_play_config(scenario_id)
            preset_exists = bool(
                play_config.model_preset_id
                and store.get_model_preset(play_config.model_preset_id)
            )

    return {
        "version": "1.1",
        "exported_at": datetime.utcnow().isoformat(),
        "scenario": {
            "title": scenario.title,
            "description": scenario.description,
            "tone_instructions": scenario.tone_instructions,
            "opening_narrative": scenario.opening_narrative,
            "language": scenario.language,
            "lore_text": scenario.lore_text,
            "setup_questions": scenario.setup_questions,
            "opening_mode": scenario.opening_mode,
            "ai_opening_directive": scenario.ai_opening_directive,
            "world_type": scenario.world_type,
        },
        "characters": [
            {
                "name": c.name, "role": c.role, "description": c.description,
                "portrait_url": c.portrait_url, "is_player": c.is_player,
                "looks": [
                    {"outfit": l.outfit, "expression": l.expression,
                     "image_url": l.image_url}
                    for l in c.looks
                ],
            }
            for c in characters
        ],
        "story_cards": [
            {"card_type": c.card_type.value, "name": c.name, "content": c.content}
            for c in story_cards
        ],
        "campaigns": [
            {"player_name": c.player_name, "setup_answers": c.setup_answers}
            for c in campaigns
        ],
        "knowledge": [
            {
                "id": entry.id,
                "layer": entry.layer,
                "title": entry.title,
                "content": entry.content,
                "tags": entry.tags,
                "depth": entry.depth,
                "always_on": entry.always_on,
                "mode_scope": entry.mode_scope,
                "enabled": entry.enabled,
                "sort_order": entry.sort_order,
            }
            for entry in knowledge
        ],
        "play_config": (
            {
                "mode": play_config.mode,
                "storyline": play_config.storyline.model_dump(mode="json"),
                "max_depth": play_config.max_depth,
                "model_preset_id": play_config.model_preset_id if preset_exists else "",
            }
            if play_config is not None
            else None
        ),
    }


@router.delete("/{scenario_id}/campaigns/{campaign_id}", status_code=200)
def delete_campaign(scenario_id: str, campaign_id: str):
    with _get_store() as store:
        deleted = store.delete_campaign(campaign_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Campaign not found")
    event_store = _get_event_store()
    try:
        event_store.delete_by_campaign(campaign_id)
    finally:
        event_store.close()
    return {"status": "ok"}


@router.delete("/{scenario_id}", status_code=200)
def delete_scenario(scenario_id: str):
    with _get_store() as store:
        # Get all campaigns to clean up their events
        campaigns = store.get_campaigns(scenario_id)
        deleted = store.delete_scenario(scenario_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scenario not found")
    event_store = _get_event_store()
    try:
        for c in campaigns:
            event_store.delete_by_campaign(c.id)
    finally:
        event_store.close()
    return {"status": "ok"}