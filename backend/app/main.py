import hmac
import logging
import mimetypes
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
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
from app.api.routes_works import router as works_router
from app.engines.llm_router import POLICY_CHOICES, LLMProvider, custom_route_available

app = FastAPI(title="Project Lunar", version="0.5.0")

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
app.include_router(works_router, prefix="/api/v1", tags=["works"])


@app.middleware("http")
async def api_token_guard(request: Request, call_next):
    """Optional bearer-token gate for every /api route.

    Off unless API_TOKEN is set. The health probe stays public so process
    supervisors and load balancers can check liveness without a secret.
    """
    token = os.environ.get("API_TOKEN", "").strip()
    if request.method == "OPTIONS":
        # CORS preflight never carries an application token; let the CORS
        # middleware answer it instead of returning 401 to the browser.
        return await call_next(request)
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
    # Content policy lives on the router rather than on a game session, so the
    # switch covers narration, generation and NPC speech alike.
    content_policy: str | None = None
    nsfw_model: str | None = None
    local_fallback_on_refusal: bool | None = None


def _normalise_policy(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value not in POLICY_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"unknown content policy {value!r}; expected one of {POLICY_CHOICES}",
        )
    return value


@app.post("/api/settings")
def update_settings(req: SettingsUpdateRequest):
    try:
        provider = LLMProvider(req.provider)
    except ValueError:
        provider = LLMProvider.DEEPSEEK
    policy = _normalise_policy(req.content_policy)
    _llm.config.temperature = req.temperature
    _llm.config.max_tokens = req.max_tokens
    if policy is not None:
        _llm.config.content_policy = policy
    if req.nsfw_model is not None:
        _llm.config.nsfw_model = req.nsfw_model.strip() or None
    if req.local_fallback_on_refusal is not None:
        _llm.config.local_fallback_on_refusal = bool(req.local_fallback_on_refusal)
    apply_model_policy(provider, req.model)
    model = _llm.config.orchestrator_model or _llm.config.primary_model
    return {
        "status": "ok",
        "provider": provider.value,
        "model": model,
        "content_policy": policy or _llm.config.content_policy,
    }


def _custom_route_model() -> str | None:
    """The model a custom endpoint would answer with, or None when unconfigured."""
    if not custom_route_available():
        return None
    from app.engines.llm_router import custom_default_model

    return custom_default_model()


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
        "content_policy": _llm.config.content_policy,
        "policy_choices": list(POLICY_CHOICES),
        "nsfw_provider": (
            _llm.config.nsfw_provider.value if _llm.config.nsfw_provider else None
        ),
        "nsfw_model": _llm.config.nsfw_model,
        "local_fallback_on_refusal": _llm.config.local_fallback_on_refusal,
        "local_route_available": _llm.local_route_available(),
        # A custom OpenAI-compatible vendor: the panel shows the switch only when
        # the base URL and key are in .env. Neither the key nor the URL is echoed.
        "custom_route_available": custom_route_available(),
        "custom_model": _custom_route_model(),
    }
