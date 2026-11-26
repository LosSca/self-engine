"""What the intent advisor learned, kept in a table.

The advisor costs one model call per utterance it has to read. The same
sentence, typed by the same player in the same world, means the same thing -
so the first successful reading is written down and the next identical
utterance is resolved from the table without a model call.

Scope is the *world*. The same words can mean different things in different
worlds ("推开门" is one door in a station and another in a tavern), while every
campaign played in one world shares the vocabulary it taught.

Nothing here decides what happens: a row stores only the action name the
advisor picked and the words it thought named the target. The caller feeds both
back through the deterministic parser and the rules engine, so a stale row -
a door renamed, an item removed - simply fails to resolve and the normal path
takes over.
"""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional


_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: A row per (world, language, sentence). Removing the envelope keeps "[DO] 推开门"
#: and "推开门" the same learned phrase.
_ENVELOPE = re.compile(r"^\s*\[[A-Za-z]+\]\s*")
_PUNCT = " \t\r\n，。！？、；：,.!?;:\"'“”‘’（）()【】[]{}<>《》…—-·"


def default_db_path() -> str:
    return os.environ.get("SCENARIO_DB_PATH", os.path.join(_BACKEND_DIR, "scenarios.db"))


def normalise_phrase(text: str) -> str:
    """The key two utterances have to share to count as the same request."""
    value = _ENVELOPE.sub("", str(text or ""))
    value = unicodedata.normalize("NFKC", value).strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip(_PUNCT)


class IntentMemoryStore:
    """Learned utterance -> (action, target words), scoped per world."""

    def __init__(self, db_path: Optional[str] = None):
        self._conn = sqlite3.connect(db_path or default_db_path(), check_same_thread=False)
        self._lock = Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS intent_learnings ("
            "    id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "    scope_key TEXT NOT NULL,"
            "    lang TEXT NOT NULL DEFAULT '',"
            "    phrase TEXT NOT NULL,"
            "    action TEXT NOT NULL,"
            "    fragment TEXT NOT NULL DEFAULT '',"
            "    confidence REAL NOT NULL DEFAULT 0,"
            "    hits INTEGER NOT NULL DEFAULT 0,"
            "    source TEXT NOT NULL DEFAULT 'advisor',"
            "    created_at TEXT NOT NULL,"
            "    updated_at TEXT NOT NULL,"
            "    UNIQUE(scope_key, lang, phrase)"
            ")"
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - closing twice must stay harmless
            pass

    # ------------------------------------------------------------------ write
    def remember(
        self,
        scope_key: str,
        phrase: str,
        action: str,
        fragment: str = "",
        *,
        lang: str = "",
        confidence: float = 0.0,
        source: str = "advisor",
    ) -> Optional[int]:
        """Write one reading down; a repeat utterance bumps its hit count."""
        key = normalise_phrase(phrase)
        scope = str(scope_key or "").strip()
        action_name = str(action or "").strip()
        if not scope or not key or not action_name:
            return None
        now = datetime.utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO intent_learnings "
                "(scope_key, lang, phrase, action, fragment, confidence, hits, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,1,?,?,?) "
                "ON CONFLICT(scope_key, lang, phrase) DO UPDATE SET "
                "action=excluded.action, fragment=excluded.fragment, "
                "confidence=excluded.confidence, hits=intent_learnings.hits+1, "
                "source=excluded.source, updated_at=excluded.updated_at",
                (scope, str(lang or ""), key, action_name, str(fragment or ""),
                 float(confidence or 0.0), str(source or "advisor"), now, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM intent_learnings WHERE scope_key=? AND lang=? AND phrase=?",
                (scope, str(lang or ""), key),
            ).fetchone()
        return int(row[0]) if row else None

    # ------------------------------------------------------------------- read
    def recall(self, scope_key: str, phrase: str, *, lang: str = "") -> Optional[Dict[str, Any]]:
        """A learned reading, or None. A hit counts as a use of the row."""
        key = normalise_phrase(phrase)
        scope = str(scope_key or "").strip()
        if not scope or not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, action, fragment, confidence, hits FROM intent_learnings "
                "WHERE scope_key=? AND lang=? AND phrase=?",
                (scope, str(lang or ""), key),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE intent_learnings SET hits=hits+1, updated_at=? WHERE id=?",
                (datetime.utcnow().isoformat(), row[0]),
            )
            self._conn.commit()
        return {
            "id": int(row[0]),
            "action": str(row[1]),
            "fragment": str(row[2]),
            "confidence": float(row[3]),
            "hits": int(row[4]) + 1,
        }

    def list(self, scope_key: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        try:
            cap = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            cap = 200
        if scope_key:
            rows = self._conn.execute(
                "SELECT id, scope_key, lang, phrase, action, fragment, confidence, hits, source, updated_at "
                "FROM intent_learnings WHERE scope_key=? ORDER BY updated_at DESC, id DESC LIMIT ?",
                (str(scope_key), cap),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, scope_key, lang, phrase, action, fragment, confidence, hits, source, updated_at "
                "FROM intent_learnings ORDER BY updated_at DESC, id DESC LIMIT ?",
                (cap,),
            ).fetchall()
        return [
            {
                "id": int(r[0]),
                "scope_key": str(r[1]),
                "lang": str(r[2]),
                "phrase": str(r[3]),
                "action": str(r[4]),
                "fragment": str(r[5]),
                "confidence": float(r[6]),
                "hits": int(r[7]),
                "source": str(r[8]),
                "updated_at": str(r[9]),
            }
            for r in rows
        ]

    def delete(self, row_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM intent_learnings WHERE id=?", (int(row_id),))
            self._conn.commit()
            return cur.rowcount > 0

    def clear(self, scope_key: Optional[str] = None) -> int:
        with self._lock:
            if scope_key:
                cur = self._conn.execute(
                    "DELETE FROM intent_learnings WHERE scope_key=?", (str(scope_key),)
                )
            else:
                cur = self._conn.execute("DELETE FROM intent_learnings")
            self._conn.commit()
            return int(cur.rowcount or 0)


def get_intent_memory_store(db_path: Optional[str] = None) -> IntentMemoryStore:
    return IntentMemoryStore(db_path)
