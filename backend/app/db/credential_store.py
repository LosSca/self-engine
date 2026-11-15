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

#: What the process environment already held when this module was first imported.
#: A UI "clear" must fall back to these, not to nothing: without this snapshot the
#: only way to undo a stored key was to drop the variable entirely, which also
#: destroyed a key the user had supplied through .env / the shell / Docker.
_ENV_AT_STARTUP: Dict[str, str] = {
    name: os.environ[name]
    for name in (
        *{spec["env"] for spec in PROVIDERS.values()},
        *URL_ENV.values(),
        *MODEL_ENV.values(),
    )
    if os.environ.get(name)
}


def _restrict_to_owner(path: str) -> None:
    """Best effort: make a file readable only by the account that owns it.

    The keys live in plain text in the same SQLite file as the preferences, so the
    file mode is the only thing standing between a key and another account on a
    shared machine. Windows ACLs and POSIX modes are set through different calls
    and both are best effort: a failure here must never stop the engine from
    starting, because on a single-user desktop the file already sits under that
    user profile.
    """
    try:
        if os.name == 'nt':
            import subprocess

            user = os.environ.get('USERNAME', '')
            if not user:
                return
            # Replace inherited permissions with one grant for this account.
            subprocess.run(
                ["icacls", path, "/inheritance:r", f"/grant:r", f"{user}:F"],
                check=False,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001 - hardening, not a precondition for starting
        pass

class CredentialStore:
    """One row per provider on the same sqlite file as the preferences."""

    def __init__(self, db_path: str | None = None):
        path = db_path or default_db_path()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = Lock()
        # Keys sit in plain text here, so the file is kept to this account only.
        _restrict_to_owner(path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS provider_credentials ("
            "    provider TEXT PRIMARY KEY,"
            "    api_key TEXT NOT NULL DEFAULT '',"
            "    base_url TEXT NOT NULL DEFAULT '',"
            "    model TEXT NOT NULL DEFAULT '',"
            # Keyless endpoints exist: a local Ollama, an intranet gateway, or a
            # vendor that authenticates by address. Without this flag such a row
            # was indistinguishable from an empty one, so "save" silently did
            # nothing and apply_credentials() restored the start-up key again.
            "    keyless INTEGER NOT NULL DEFAULT 0,"
            "    updated_at TEXT NOT NULL"
            ")"
        )
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(provider_credentials)")
        }
        if "keyless" not in columns:
            # Table created before the flag existed: the difference between
            # "never saved" and "saved without a key" is exactly a stored URL.
            self._conn.execute(
                "ALTER TABLE provider_credentials ADD COLUMN keyless INTEGER NOT NULL DEFAULT 0"
            )
            self._conn.execute(
                "UPDATE provider_credentials SET keyless = 1"
                " WHERE api_key = '' AND base_url <> ''"
            )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - closing twice must stay harmless
            pass

    def rows(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        for provider, api_key, base_url, model, keyless in self._conn.execute(
            "SELECT provider, api_key, base_url, model, keyless FROM provider_credentials"
        ).fetchall():
            out[provider] = {
                "api_key": api_key,
                "base_url": base_url,
                "model": model,
                "keyless": bool(keyless),
            }
        return out

    def put(self, provider: str, *, api_key: str | None, base_url: str | None, model: str | None) -> None:
        from datetime import datetime

        current = self.rows().get(
            provider, {"api_key": "", "base_url": "", "model": "", "keyless": False}
        )
        merged = {
            "api_key": current["api_key"] if api_key is None else api_key,
            "base_url": current["base_url"] if base_url is None else base_url,
            "model": current["model"] if model is None else model,
        }
        # A row created by this write without a key is deliberate: the endpoint
        # does not need one. A later write that supplies a key clears the flag,
        # so "save an address now, the key tomorrow" converges as expected.
        keyless = current["keyless"] and api_key is None
        if api_key is not None and not api_key:
            keyless = True
        with self._lock:
            self._conn.execute(
                "INSERT INTO provider_credentials (provider, api_key, base_url, model, keyless, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET "
                "api_key=excluded.api_key, base_url=excluded.base_url, "
                "model=excluded.model, keyless=excluded.keyless, updated_at=excluded.updated_at",
                (
                    provider,
                    merged["api_key"],
                    merged["base_url"],
                    merged["model"],
                    1 if keyless else 0,
                    datetime.utcnow().isoformat(),
                ),
            )
            self._conn.commit()

    def delete(self, provider: str) -> None:
        """Drop the stored row only.

        The process environment is *not* touched here. Restoring it is the job of
        apply_credentials(), which puts back whatever .env / the shell supplied
        before the UI ever wrote anything. Popping the variable here used to wipe
        an environment-provided key for the rest of the process lifetime — see
        test_clearing_a_ui_key_restores_the_environment_key.
        """
        with self._lock:
            self._conn.execute("DELETE FROM provider_credentials WHERE provider=?", (provider,))
            self._conn.commit()


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

    Called at startup and after every write, including a delete. A stored row
    wins; when there is no row, the variable is restored to whatever the process
    started with (.env / shell / Docker), so clearing a UI key falls back instead
    of permanently disabling the provider.
    """
    store = CredentialStore()
    try:
        rows = store.rows()
    finally:
        store.close()
    # Iterate every known provider, not just the stored ones: a provider with no
    # row still needs its start-up value put back (that is the "clear" case).
    for provider, spec in PROVIDERS.items():
        row = rows.get(provider, {})
        for env_name, column in (
            (spec["env"], "api_key"),
            (URL_ENV.get(provider), "base_url"),
            (MODEL_ENV.get(provider), "model"),
        ):
            if not env_name:
                continue
            value = row.get(column) or ""
            # A saved row owns this variable even when the value is empty: a
            # keyless endpoint needs the key variable to be *absent*, and
            # falling back to the start-up value here put a stale key back.
            owns_variable = bool(row) and (column != "api_key" or row.get("keyless"))
            if value:
                os.environ[env_name] = value
            elif owns_variable:
                os.environ.pop(env_name, None)
            elif env_name in _ENV_AT_STARTUP:
                os.environ[env_name] = _ENV_AT_STARTUP[env_name]
            else:
                os.environ.pop(env_name, None)


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
        # An address with no key is a complete configuration for a keyless
        # endpoint; reporting it as "not configured" hid a working setup.
        keyless = bool(row.get("keyless")) and bool(row.get("base_url"))
        from_env = bool(os.environ.get(spec["env"]))
        out.append(
            {
                "id": provider,
                "label": spec["label"],
                "help": spec["help"],
                "env": spec["env"],
                "configured": from_ui or keyless or from_env,
                "source": (
                    "settings" if (from_ui or keyless) else ("env" if from_env else "")
                ),
                "preview": masked(row.get("api_key", "") or os.environ.get(spec["env"], "")),
                "base_url": row.get("base_url", ""),
                "model": row.get("model", ""),
                "keyless": keyless,
                "editable_base_url": provider in URL_ENV,
            }
        )
    return out