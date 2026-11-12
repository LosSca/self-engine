"""Model provider credentials entered in the UI.

Until now a key could only arrive through `.env` or the shell environment, so a
player running the desktop build had no way to configure a provider at all —
the settings panel could pick a provider name but not connect one. These rows
live beside the other user preferences and are applied to `os.environ`, which
is what litellm reads.

Write-only: no endpoint ever returns a stored key. `GET` reports whether one
is present (and a masked tail), never the value itself.
"""

import json
import os
import sqlite3
from threading import Lock
from typing import Any, Dict

from app.db.prefs_store import default_db_path

#: provider id -> (env var litellm reads, label, hint for the UI)
PROVIDERS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "env": "DEEPSEEK_API_KEY",
        "label": "DeepSeek",
        "help": "在 platform.deepseek.com 申请；默认线路就是它。",
    },
    "openai": {
        "env": "OPENAI_API_KEY",
        "label": "OpenAI",
        "help": "在 platform.openai.com 申请；也可以填任何兼容 OpenAI 协议的代理地址。",
    },
    "anthropic": {
        "env": "ANTHROPIC_API_KEY",
        "label": "Anthropic",
        "help": "在 console.anthropic.com 申请。",
    },
    "local": {
        "env": "LOCAL_LLM_API_KEY",
        "label": "本地 NInfer",
        "help": "本地兼容 OpenAI 的服务（如 127.0.0.1:8094），密钥随便填或留空都行。",
    },
    "custom": {
        "env": "CUSTOM_LLM_API_KEY",
        "label": "自定义 OpenAI 兼容服务",
        "help": "任何兼容 OpenAI 协议的第三方服务：填地址、密钥和模型名。",
    },
}

#: non-secret row written alongside a key (never returned verbatim either)
URL_ENV = {
    "local": "LOCAL_LLM_BASE_URL",
    "custom": "CUSTOM_LLM_BASE_URL",
}
MODEL_ENV = {
    "local": "LOCAL_LLM_MODEL",
    "custom": "CUSTOM_LLM_MODEL",
}


class CredentialStore:
    """One row per provider on the same sqlite file as the preferences."""

    def __init__(self, db_path: str | None = None):
        self._conn = sqlite3.connect(db_path or default_db_path(), check_same_thread=False)
        self._lock = Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS provider_credentials ("
            "    provider TEXT PRIMARY KEY,"
            "    api_key TEXT NOT NULL DEFAULT '',"
            "    base_url TEXT NOT NULL DEFAULT '',"
            "    model TEXT NOT NULL DEFAULT '',"
            "    updated_at TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - closing twice must stay harmless
            pass

    def rows(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        for provider, api_key, base_url, model in self._conn.execute(
            "SELECT provider, api_key, base_url, model FROM provider_credentials"
        ).fetchall():
            out[provider] = {"api_key": api_key, "base_url": base_url, "model": model}
        return out

    def put(self, provider: str, *, api_key: str | None, base_url: str | None, model: str | None) -> None:
        from datetime import datetime

        current = self.rows().get(provider, {"api_key": "", "base_url": "", "model": ""})
        merged = {
            "api_key": current["api_key"] if api_key is None else api_key,
            "base_url": current["base_url"] if base_url is None else base_url,
            "model": current["model"] if model is None else model,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO provider_credentials (provider, api_key, base_url, model, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                "api_key=excluded.api_key, base_url=excluded.base_url, "
                "model=excluded.model, updated_at=excluded.updated_at",
                (provider, merged["api_key"], merged["base_url"], merged["model"], datetime.utcnow().isoformat()),
            )
            self._conn.commit()

    def delete(self, provider: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM provider_credentials WHERE provider=?", (provider,))
            self._conn.commit()
        # 删掉之后 os.environ 里还留着刚才写进去的 key，会让「已清除」变成假象。
        # 这里直接摘掉：shell/Docker 提供的变量会在下次启动时重新进环境。
        spec = PROVIDERS.get(provider)
        if spec:
            for env_name in (spec["env"], URL_ENV.get(provider), MODEL_ENV.get(provider)):
                if env_name:
                    os.environ.pop(env_name, None)


def masked(key: str) -> str:
    """A key shown back to its owner, stripped to the last four characters."""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return "…" + key[-4:]


def apply_credentials() -> None:
    """Push stored credentials into os.environ (what litellm reads).

    Called at startup and after every write. An existing environment value
    wins only when nothing was stored, so a shell/Docker deployment keeps
    working exactly as before.
    """
    store = CredentialStore()
    try:
        rows = store.rows()
    finally:
        store.close()
    for provider, row in rows.items():
        spec = PROVIDERS.get(provider)
        if spec is None:
            continue
        if row.get("api_key"):
            os.environ[spec["env"]] = row["api_key"]
        url_env = URL_ENV.get(provider)
        if url_env and row.get("base_url"):
            os.environ[url_env] = row["base_url"]
        model_env = MODEL_ENV.get(provider)
        if model_env and row.get("model"):
            os.environ[model_env] = row["model"]


def status() -> list[dict[str, Any]]:
    """Per provider: is it usable right now, and where does that come from."""
    store = CredentialStore()
    try:
        rows = store.rows()
    finally:
        store.close()
    out: list[dict[str, Any]] = []
    for provider, spec in PROVIDERS.items():
        row = rows.get(provider, {})
        from_ui = bool(row.get("api_key"))
        from_env = bool(os.environ.get(spec["env"]))
        out.append(
            {
                "id": provider,
                "label": spec["label"],
                "help": spec["help"],
                "env": spec["env"],
                "configured": from_ui or from_env,
                "source": "settings" if from_ui else ("env" if from_env else ""),
                "preview": masked(row.get("api_key", "") or os.environ.get(spec["env"], "")),
                "base_url": row.get("base_url", ""),
                "model": row.get("model", ""),
                "editable_base_url": provider in URL_ENV,
            }
        )
    return out