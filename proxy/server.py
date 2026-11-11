"""FastAPI proxy server — forwards Anthropic Messages API requests using OAuth tokens."""

import json
import logging
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
import httpx

from auth import OAuthFlow
from config import (
    API_BASE, ANTHROPIC_VERSION, ANTHROPIC_BETA,
    REQUEST_TIMEOUT, MODELS, DEFAULT_MODEL,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Claude Max Proxy", version="1.0.0")
oauth = OAuthFlow()


def _build_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": ANTHROPIC_BETA,
        "Content-Type": "application/json",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "has_token": not oauth.store.is_expired()}


@app.get("/v1/models")
async def list_models():
    data = []
    for model_id, meta in MODELS.items():
        data.append({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "anthropic",
            "context_length": meta["context"],
            "max_output_tokens": meta["max_output"],
        })
    return {"object": "list", "data": data}


@app.post("/v1/messages")
async def messages_proxy(request: Request):
    """Proxy Anthropic Messages API — non-streaming and streaming."""
    token = await oauth.get_valid_token()
    if not token:
        raise HTTPException(401, "No valid OAuth token. Run: python run.py auth")

    body = await request.json()
    is_stream = body.get("stream", False)

    if is_stream:
        return StreamingResponse(
            _stream_response(body, token),
            media_type="text/event-stream",
        )

    # Non-streaming
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{API_BASE}/v1/messages",
            headers=_build_headers(token),
            json=body,
        )

    if resp.status_code == 401:
        # Try refresh once
        if await oauth.refresh():
            new_token = oauth.store.get_access_token()
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{API_BASE}/v1/messages",
                    headers=_build_headers(new_token),
                    json=body,
                )

    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.json())

    return JSONResponse(resp.json())


async def _stream_response(body: dict, token: str):
    """Stream SSE from Anthropic, forwarding as-is."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{API_BASE}/v1/messages",
            headers=_build_headers(token),
            json=body,
        ) as resp:
            if resp.status_code == 401:
                if await oauth.refresh():
                    token = oauth.store.get_access_token()
                    async with client.stream(
                        "POST",
                        f"{API_BASE}/v1/messages",
                        headers=_build_headers(token),
                        json=body,
                    ) as retry_resp:
                        async for line in retry_resp.aiter_lines():
                            yield line + "\n"
                    return

            if resp.status_code != 200:
                error = await resp.aread()
                logger.error("Anthropic error %d: %s", resp.status_code, error.decode())
                yield f"data: {json.dumps({'error': error.decode()})}\n\n"
                return

            async for line in resp.aiter_lines():
                yield line + "\n"
