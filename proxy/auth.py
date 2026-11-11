"""OAuth PKCE authentication for Claude Pro/Max subscription."""

import base64
import hashlib
import json
import secrets
import time
import webbrowser
import logging
from pathlib import Path

import httpx

from config import (
    AUTH_AUTHORIZE_URL, AUTH_TOKEN_URL, CLIENT_ID,
    REDIRECT_URI, SCOPES, TOKEN_FILE,
)

logger = logging.getLogger(__name__)


class TokenStore:
    """Persists OAuth tokens to disk."""

    def __init__(self, path: Path = TOKEN_FILE):
        self.path = path

    def save(self, access_token: str, refresh_token: str, expires_in: int):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": int(time.time()) + expires_in,
        }
        self.path.write_text(json.dumps(data, indent=2))

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def get_access_token(self) -> str | None:
        data = self.load()
        return data["access_token"] if data else None

    def get_refresh_token(self) -> str | None:
        data = self.load()
        return data.get("refresh_token") if data else None

    def is_expired(self) -> bool:
        data = self.load()
        if not data:
            return True
        return int(time.time()) >= data.get("expires_at", 0) - 60

    def clear(self):
        if self.path.exists():
            self.path.unlink()


class OAuthFlow:
    """OAuth PKCE flow for Claude Pro/Max."""

    def __init__(self):
        self.store = TokenStore()
        self._verifier: str | None = None

    def _generate_pkce(self) -> tuple[str, str]:
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        return verifier, challenge

    def start_login(self) -> str:
        """Open browser for OAuth login. Returns the authorize URL."""
        self._verifier, challenge = self._generate_pkce()
        params = {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": self._verifier,
        }
        from urllib.parse import urlencode
        url = f"{AUTH_AUTHORIZE_URL}?{urlencode(params)}"
        webbrowser.open(url)
        return url

    async def exchange_code(self, raw_code: str):
        """Exchange authorization code for tokens."""
        parts = raw_code.strip().split("#")
        code = parts[0]
        state = parts[1] if len(parts) > 1 else self._verifier

        if not self._verifier:
            raise ValueError("No PKCE verifier — run start_login() first")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                AUTH_TOKEN_URL,
                json={
                    "code": code,
                    "state": state,
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": self._verifier,
                },
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        self.store.save(data["access_token"], data["refresh_token"], data.get("expires_in", 3600))
        self._verifier = None
        logger.info("OAuth tokens saved successfully")

    async def refresh(self) -> bool:
        """Refresh expired tokens. Returns True on success."""
        refresh_token = self.store.get_refresh_token()
        if not refresh_token:
            return False

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    AUTH_TOKEN_URL,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": CLIENT_ID,
                    },
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code != 200:
                logger.error("Token refresh failed: %s", resp.text)
                return False

            data = resp.json()
            self.store.save(data["access_token"], data["refresh_token"], data.get("expires_in", 3600))
            logger.info("Tokens refreshed")
            return True
        except Exception as e:
            logger.error("Token refresh error: %s", e)
            return False

    async def get_valid_token(self) -> str | None:
        """Get a valid access token, auto-refreshing if needed."""
        if not self.store.is_expired():
            return self.store.get_access_token()

        logger.info("Token expired, refreshing...")
        if await self.refresh():
            return self.store.get_access_token()
        return None
