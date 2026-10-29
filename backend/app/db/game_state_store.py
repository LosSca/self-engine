"""SQLite persistence for world definitions, authoritative campaign state,
action requests and domain events.

Design notes (phase 1):
- SQLite owns the authoritative game state. Neo4j stays a knowledge layer and is
  never asked to hold the same state twice.
- The state snapshot, the request row and the domain events for one action are
  written inside a single transaction, so a crash cannot leave the state applied
  without its audit trail. A rejected or duplicate request writes only the
  request row.
- request_id is the primary key of action_requests, which makes the storage layer
  itself the last line of defence against repeated side effects.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.models.game import GameState, WorldDefinition

SCHEMA_VERSION = 2

_IDX_REQUESTS = (
    "CREATE INDEX IF NOT EXISTS idx_action_requests_campaign "
    "ON action_requests(campaign_id, created_at)"
)
_IDX_EVENTS = (
    "CREATE INDEX IF NOT EXISTS idx_domain_events_campaign "
    "ON domain_events(campaign_id, created_at)"
)

_MIGRATIONS: Dict[int, List[str]] = {
    1: [
        "CREATE TABLE IF NOT EXISTS worlds ("
        "    world_id TEXT PRIMARY KEY,"
        "    scenario_id TEXT NOT NULL DEFAULT '',"
        "    name TEXT NOT NULL,"
        "    definition_json TEXT NOT NULL,"
        "    created_at TEXT NOT NULL"
        ")",
        "CREATE TABLE IF NOT EXISTS campaign_state ("
        "    campaign_id TEXT PRIMARY KEY,"
        "    world_id TEXT NOT NULL,"
        "    version INTEGER NOT NULL DEFAULT 1,"
        "    state_json TEXT NOT NULL,"
        "    updated_at TEXT NOT NULL"
        ")",
        "CREATE TABLE IF NOT EXISTS action_requests ("
        "    request_id TEXT PRIMARY KEY,"
        "    campaign_id TEXT NOT NULL,"
        "    request_json TEXT NOT NULL,"
        "    status TEXT NOT NULL,"
        "    result_json TEXT NOT NULL,"
        "    state_version INTEGER NOT NULL DEFAULT 0,"
        "    created_at TEXT NOT NULL"
        ")",
        "CREATE TABLE IF NOT EXISTS domain_events ("
        "    id TEXT PRIMARY KEY,"
        "    campaign_id TEXT NOT NULL,"
        "    request_id TEXT NOT NULL DEFAULT '',"
        "    event_type TEXT NOT NULL,"
        "    payload_json TEXT NOT NULL,"
        "    state_version INTEGER NOT NULL DEFAULT 0,"
        "    created_at TEXT NOT NULL"
        ")",
        _IDX_REQUESTS,
        _IDX_EVENTS,
    ],
    2: [
        "CREATE TABLE IF NOT EXISTS campaign_snapshots ("
        "    campaign_id TEXT NOT NULL,"
        "    version INTEGER NOT NULL,"
        "    request_id TEXT NOT NULL DEFAULT '',"
        "    state_json TEXT NOT NULL,"
        "    created_at TEXT NOT NULL,"
        "    PRIMARY KEY (campaign_id, version)"
        ")",
        "CREATE INDEX IF NOT EXISTS idx_campaign_snapshots_campaign "
        "ON campaign_snapshots(campaign_id, version)",
    ],
}


def _now() -> str:
    return datetime.utcnow().isoformat()


class GameStateStore:
    """Thin, explicit SQLite store. No ORM, no hidden global state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._migrate()

    # ------------------------------------------------------------------ schema
    def _get_schema_version(self) -> int:
        cursor = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if cursor.fetchone() is None:
            return 0
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "    version INTEGER PRIMARY KEY,"
                "    applied_at TEXT NOT NULL"
                ")"
            )
            current = self._get_schema_version()
            for version in sorted(_MIGRATIONS):
                if version <= current:
                    continue
                for statement in _MIGRATIONS[version]:
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )
            self._conn.commit()

    # ------------------------------------------------------------------ worlds
    def save_world(self, world: WorldDefinition) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO worlds (world_id, scenario_id, name, definition_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(world_id) DO UPDATE SET"
                "   scenario_id=excluded.scenario_id,"
                "   name=excluded.name,"
                "   definition_json=excluded.definition_json",
                (
                    world.world_id,
                    world.scenario_id or "",
                    world.name,
                    world.model_dump_json(),
                    _now(),
                ),
            )

    def delete_world(self, world_id: str) -> bool:
        """Remove a world definition; campaigns keep their own state rows.

        Campaigns bound to the deleted world are not cascade-deleted: their
        history and snapshots stay inspectable, and re-creating a world with the
        same id makes them playable again.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM worlds WHERE world_id = ?", (world_id,))
            return cursor.rowcount > 0

    def get_world(self, world_id: str) -> Optional[WorldDefinition]:
        row = self._conn.execute(
            "SELECT definition_json FROM worlds WHERE world_id = ?", (world_id,)
        ).fetchone()
        if row is None:
            return None
        return WorldDefinition.model_validate_json(row["definition_json"])

    def list_worlds(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT world_id, scenario_id, name, created_at FROM worlds ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ state
    def get_state(self, campaign_id: str) -> Optional[GameState]:
        row = self._conn.execute(
            "SELECT state_json FROM campaign_state WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            return None
        return GameState.model_validate_json(row["state_json"])

    def bind_campaign(self, state: GameState) -> None:
        """Create or reset the campaign state (used when a world is bound)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO campaign_state (campaign_id, world_id, version, state_json, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(campaign_id) DO UPDATE SET"
                "   world_id=excluded.world_id,"
                "   version=excluded.version,"
                "   state_json=excluded.state_json,"
                "   updated_at=excluded.updated_at",
                (
                    state.campaign_id,
                    state.world_id,
                    state.version,
                    state.model_dump_json(),
                    _now(),
                ),
            )
            self._insert_snapshot(state, request_id="")

    # --------------------------------------------------------------- requests
    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM action_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "campaign_id": row["campaign_id"],
            "request": json.loads(row["request_json"]),
            "status": row["status"],
            "result": json.loads(row["result_json"]),
            "state_version": row["state_version"],
            "created_at": row["created_at"],
        }

    def record_request(
        self,
        *,
        campaign_id: str,
        request_id: str,
        request_payload: Dict[str, Any],
        status: str,
        result_payload: Dict[str, Any],
        state_version: int,
    ) -> None:
        """Persist a request that changed no state (rejected / duplicate / error)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO action_requests"
                " (request_id, campaign_id, request_json, status, result_json,"
                "  state_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    campaign_id,
                    json.dumps(request_payload, sort_keys=True),
                    status,
                    json.dumps(result_payload, sort_keys=True),
                    state_version,
                    _now(),
                ),
            )

    def commit_action(
        self,
        *,
        campaign_id: str,
        request_id: str,
        request_payload: Dict[str, Any],
        status: str,
        result_payload: Dict[str, Any],
        state: GameState,
        events: List[Dict[str, Any]],
    ) -> None:
        """Commit the request row, the new state and the domain events atomically.

        The request row is inserted first: a duplicate request_id raises
        sqlite3.IntegrityError and the whole transaction (including the state
        update) rolls back.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO action_requests"
                " (request_id, campaign_id, request_json, status, result_json,"
                "  state_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    campaign_id,
                    json.dumps(request_payload, sort_keys=True),
                    status,
                    json.dumps(result_payload, sort_keys=True),
                    state.version,
                    _now(),
                ),
            )
            updated = self._conn.execute(
                "UPDATE campaign_state SET version = ?, state_json = ?, updated_at = ?"
                " WHERE campaign_id = ?",
                (state.version, state.model_dump_json(), _now(), campaign_id),
            )
            if updated.rowcount == 0:
                raise ValueError(
                    f"campaign '{campaign_id}' has no bound state; bind a world first"
                )
            self._insert_snapshot(state, request_id=request_id)
            for event in events:
                self._conn.execute(
                    "INSERT INTO domain_events"
                    " (id, campaign_id, request_id, event_type, payload_json,"
                    "  state_version, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.get("id") or str(uuid.uuid4()),
                        campaign_id,
                        request_id,
                        event["event_type"],
                        json.dumps(event.get("payload", {}), sort_keys=True),
                        event.get("state_version", state.version),
                        event.get("created_at") or _now(),
                    ),
                )

    # ------------------------------------------------------------- snapshots
    def _insert_snapshot(
        self, state: GameState, request_id: str = "", created_at: Optional[str] = None
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO campaign_snapshots"
            " (campaign_id, version, request_id, state_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                state.campaign_id,
                state.version,
                request_id,
                state.model_dump_json(),
                created_at or _now(),
            ),
        )

    def list_snapshots(self, campaign_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT version, request_id, created_at FROM campaign_snapshots"
            " WHERE campaign_id = ? ORDER BY version DESC LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_snapshot(self, campaign_id: str, version: int) -> Optional[GameState]:
        row = self._conn.execute(
            "SELECT state_json FROM campaign_snapshots"
            " WHERE campaign_id = ? AND version = ?",
            (campaign_id, version),
        ).fetchone()
        if row is None:
            return None
        return GameState.model_validate_json(row["state_json"])

    def commit_rollback(
        self,
        *,
        campaign_id: str,
        state: GameState,
        events: List[Dict[str, Any]],
        request_id: str = "",
    ) -> None:
        """Write a restored state as a brand-new version (history stays intact)."""
        with self._lock, self._conn:
            updated = self._conn.execute(
                "UPDATE campaign_state SET version = ?, state_json = ?, updated_at = ?"
                " WHERE campaign_id = ?",
                (state.version, state.model_dump_json(), _now(), campaign_id),
            )
            if updated.rowcount == 0:
                raise ValueError(f"campaign '{campaign_id}' has no bound state")
            self._insert_snapshot(state, request_id=request_id)
            for event in events:
                self._conn.execute(
                    "INSERT INTO domain_events"
                    " (id, campaign_id, request_id, event_type, payload_json,"
                    "  state_version, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.get("id") or str(uuid.uuid4()),
                        campaign_id,
                        request_id,
                        event["event_type"],
                        json.dumps(event.get("payload", {}), sort_keys=True),
                        event.get("state_version", state.version),
                        event.get("created_at") or _now(),
                    ),
                )

    # ---------------------------------------------------------------- events
    def list_events(
        self, campaign_id: str, limit: int = 50, after_version: int = 0
    ) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM domain_events"
            " WHERE campaign_id = ? AND state_version > ?"
            " ORDER BY rowid ASC LIMIT ?",
            (campaign_id, after_version, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "campaign_id": row["campaign_id"],
                "request_id": row["request_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "state_version": row["state_version"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_domain_events(
        self,
        campaign_id: str,
        *,
        event_type_prefix: str = "",
        limit: int = 100,
        after_version: int = 0,
    ) -> List[Dict[str, Any]]:
        """Most recent first, optionally filtered by an event_type prefix.

        `list_events` is the forward (replay) view used by clients that poll
        incrementally; history screens and debug consoles want the newest rows
        instead, which is what `LIMIT` should mean here.
        """
        sql = (
            "SELECT * FROM domain_events"
            " WHERE campaign_id = ? AND state_version > ?"
        )
        params: List[Any] = [campaign_id, after_version]
        if event_type_prefix:
            sql += " AND event_type LIKE ?"
            params.append(event_type_prefix.replace("%", "") + "%")
        sql += " ORDER BY rowid DESC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "id": row["id"],
                "campaign_id": row["campaign_id"],
                "request_id": row["request_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "state_version": row["state_version"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_action_request_result(
        self, campaign_id: str, request_id: str
    ) -> Optional[Dict[str, Any]]:
        row = self.get_request(request_id)
        if row is not None and row["campaign_id"] != campaign_id:
            return None
        return row

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "GameStateStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()