import hmac
import logging
import mimetypes
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings

if settings.debug:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)
from app.api.routes_scenarios import router as scenarios_router
from app.api.routes_game import router as game_router, _llm, apply_model_policy
from app.api.routes_engine import router as engine_router
from app.api.routes_characters import router as characters_router
from app.api.routes_knowledge import router as knowledge_router
from app.engines.llm_router import LLMProvider

app = FastAPI(title="Project Lunar", version="0.4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scenarios_router, prefix="/api/scenarios", tags=["scenarios"])
app.include_router(game_router, prefix="/api/game", tags=["game"])
app.include_router(engine_router, prefix="/api/v1", tags=["engine"])
app.include_router(characters_router, prefix="/api", tags=["characters"])
app.include_router(knowledge_router, prefix="/api/v1", tags=["knowledge"])


@app.middleware("http")
async def api_token_guard(request: Request, call_next):
    """Optional bearer-token gate for every /api route.

    Off unless API_TOKEN is set. The health probe stays public so process
    supervisors and load balancers can check liveness without a secret.
    """
    token = os.environ.get("API_TOKEN", "").strip()
    if (
        token
        and request.url.path.startswith("/api")
        and request.url.path != "/api/health"
    ):
        supplied = request.headers.get("authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = request.headers.get("x-api-token", "").strip()
        if not hmac.compare_digest(supplied, token):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)

# Windows registry often lacks these; StaticFiles would fall back to text/plain.
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")

_media_root = Path(settings.media_dir).expanduser().resolve()
_media_root.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(_media_root)), name="media")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/health/neo4j")
async def health_neo4j():
    uri = settings.neo4j_uri
    user = settings.neo4j_user
    password = settings.neo4j_password
    try:
        from neo4j import AsyncGraphDatabase
        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        async with driver.session() as session:
            await session.run("RETURN 1")
        await driver.close()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "unavailable", "error": str(e)}


class SettingsUpdateRequest(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    temperature: float = 0.85
    max_tokens: int = 2000


@app.post("/api/settings")
def update_settings(req: SettingsUpdateRequest):
    try:
        provider = LLMProvider(req.provider)
    except ValueError:
        provider = LLMProvider.DEEPSEEK
    _llm.config.temperature = req.temperature
    _llm.config.max_tokens = req.max_tokens
    apply_model_policy(provider, req.model)
    model = _llm.config.orchestrator_model or _llm.config.primary_model
    return {"status": "ok", "provider": provider.value, "model": model}


@app.get("/api/settings")
def get_settings():
    return {
        "provider": _llm.config.primary_provider.value,
        "model": _llm.config.orchestrator_model or _llm.config.primary_model,
        "auxiliary_model": _llm.config.primary_model,
        "temperature": _llm.config.temperature,
        "max_tokens": _llm.config.max_tokens,
        "local_model": settings.local_llm_model,
        "local_base_url": settings.local_llm_base_url,
    }
