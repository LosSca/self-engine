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

# 允许的来源：vite dev（5173）、静态托管（4173），以及桌面壳的 tauri://localhost。
# 需要额外来源时用 LUNAR_CORS_ORIGINS（逗号分隔）追加，桌面壳就是这样起后端的。
_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:4173",
] + [o.strip() for o in os.environ.get("LUNAR_CORS_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
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
    # Browser-side preferences that used to live only in localStorage. They are
    # stored as-is so another browser (or the desktop WebView) sees the same
    # settings; nothing here is secret.
    theme: str | None = None
    nsfw_mode: bool | None = None
    combat_enabled: bool | None = None
    custom_model: str | None = None
    #: User-authored narration/style directive applied to every world.
    default_narrator_prompt: str | None = None


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
    stored = _save_prefs(
        {
            "provider": provider.value,
            "model": model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "content_policy": policy or _llm.config.content_policy,
            "nsfw_mode": req.nsfw_mode,
            "local_fallback_on_refusal": req.local_fallback_on_refusal,
            "combat_enabled": req.combat_enabled,
            "theme": req.theme,
            "custom_model": req.custom_model,
            "default_narrator_prompt": req.default_narrator_prompt,
        }
    )
    return {
        "status": "ok",
        "provider": provider.value,
        "model": model,
        "content_policy": policy or _llm.config.content_policy,
        "prefs": stored,
    }


def _save_prefs(values: dict) -> dict:
    """Persist the browser-side preferences, ignoring unchecked/absent fields."""
    from app.db.prefs_store import get_prefs_store

    cleaned = {k: v for k, v in values.items() if v is not None}
    if not cleaned:
        return {}
    store = get_prefs_store()
    try:
        return store.put(cleaned)
    finally:
        store.close()


def _load_prefs() -> dict:
    from app.db.prefs_store import get_prefs_store

    store = get_prefs_store()
    try:
        return store.all()
    finally:
        store.close()


def _custom_route_model() -> str | None:
    """The model a custom endpoint would answer with, or None when unconfigured."""
    if not custom_route_available():
        return None
    from app.engines.llm_router import custom_default_model

    return custom_default_model()


class PrefsUpdateRequest(BaseModel):
    """Partial write: only the keys the client sends are stored."""

    model_config = {"extra": "allow"}


@app.get("/api/prefs")
def get_prefs():
    return {"prefs": _load_prefs()}


@app.post("/api/prefs")
def update_prefs(req: PrefsUpdateRequest):
    from app.db.prefs_store import ALLOWED_KEYS

    # extra="allow" 的字段落在 __pydantic_extra__ 里，model_dump() 不会带出来，
    # 所以要和声明字段合并后再按白名单过滤。
    sent = {**req.model_dump(), **(req.__pydantic_extra__ or {})}
    payload = {k: v for k, v in sent.items() if k in ALLOWED_KEYS}
    if not payload:
        raise HTTPException(status_code=400, detail="no known preference keys")
    return {"status": "ok", "prefs": _save_prefs(payload)}


@app.get("/api/settings")
def get_settings():
    prefs = _load_prefs()
    return {
        "prefs": prefs,
        "default_narrator_prompt": prefs.get("default_narrator_prompt", ""),
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
