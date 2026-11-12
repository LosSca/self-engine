"""Cross-browser user preferences.

Settings used to live only in the browser's localStorage, so a user who opened
the app on another machine (or a desktop shell with a fresh WebView profile)
lost the provider, temperature and theme. This module keeps the same values on
the backend, next to the scenario database, so every client sees one truth.

Only non-secret values are stored: API tokens stay in the browser because a
shared backend must never hand a key back to whoever asks for /api/settings.
"""

import json
import os
import sqlite3
from threading import Lock
from typing import Any, Dict


_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Keys the frontend may persist. Keep this list explicit: the table is a
#: key/value store, and an unknown key would otherwise be a silent data sink.
ALLOWED_KEYS = (
    "provider",
    "model",
    "temperature",
    "max_tokens",
    "content_policy",
    "nsfw_mode",
    "local_fallback_on_refusal",
    "combat_enabled",
    "theme",
    "custom_model",
    #: 全局写作指令：没有任何游戏内设定时，叙事用它当 system prompt 补充。
    "default_narrator_prompt",
)


def default_db_path() -> str:
    return os.environ.get(
        "SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db")
    )


class PrefsStore:
    """Tiny key/value table on the same sqlite file as the scenarios."""

    def __init__(self, db_path: str | None = None):
        self._conn = sqlite3.connect(db_path or default_db_path(), check_same_thread=False)
        self._lock = Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS user_prefs ("
            "    key TEXT PRIMARY KEY,"
            "    value TEXT NOT NULL,"
            "    updated_at TEXT NOT NULL"
            ")"
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - closing twice must stay harmless
            pass

    def all(self) -> Dict[str, Any]:
        rows = self._conn.execute("SELECT key, value FROM user_prefs").fetchall()
        out: Dict[str, Any] = {}
        for key, raw in rows:
            try:
                out[key] = json.loads(raw)
            except Exception:  # noqa: BLE001 - a corrupt row must not break the API
                continue
        return out

    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM user_prefs WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except Exception:  # noqa: BLE001
            return default

    def put(self, values: Dict[str, Any]) -> Dict[str, Any]:
        from datetime import datetime

        now = datetime.utcnow().isoformat()
        with self._lock:
            for key, value in values.items():
                if key not in ALLOWED_KEYS:
                    continue
                self._conn.execute(
                    "INSERT INTO user_prefs (key, value, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, json.dumps(value), now),
                )
            self._conn.commit()
        return self.all()


def get_prefs_store() -> PrefsStore:
    return PrefsStore()
