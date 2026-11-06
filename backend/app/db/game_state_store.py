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
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.models.game import GameState, WorldDefinition

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

def _worlds_disagree(snapshot_json: str, current_world_id: str) -> bool:
    """True when a stored snapshot described a different world than the live one."""
    try:
        payload = json.loads(snapshot_json)
    except (TypeError, ValueError):
        return False
    stored = payload.get("world_id")
    return bool(stored) and bool(current_world_id) and stored != current_world_id


#: Audit event types written by storage maintenance rather than by gameplay.
_HOUSEKEEPING_EVENTS = ("snapshot.pruned",)

#: Seconds a writer waits for another writer's transaction before giving up.
BUSY_TIMEOUT_SECONDS = 10.0

#: Snapshot retention. Every turn writes a full state copy (~13 KB for the
#: sample village), so keeping all of them grows without bound. The newest
#: SNAPSHOT_KEEP_RECENT versions are always kept (rollback needs them), the
#: oldest ones are always kept (they are the only record of the opening state),
#: and in between only every SNAPSHOT_KEEP_EVERY-th version survives.
SNAPSHOT_KEEP_RECENT = 20
SNAPSHOT_KEEP_OLDEST = 5
SNAPSHOT_KEEP_EVERY = 10

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


class StateConflictError(RuntimeError):
    """The stored state moved on since the caller read it."""

    def __init__(self, campaign_id: str, expected_version: int, actual_version: int):
        self.campaign_id = campaign_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"campaign '{campaign_id}' is at version {actual_version}, "
            f"expected {expected_version}"
        )


class GameStateStore:
    """Thin, explicit SQLite store. No ORM, no hidden global state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # A concurrent writer waits instead of failing with "database is locked".
        self._conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False)
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

    def bind_campaign(
        self,
        state: GameState,
        *,
        clear_history: bool = False,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Create or reset the campaign state (used when a world is bound).

        ``clear_history`` is for restoring a save over an existing campaign:
        the old request/event/snapshot timeline must not leak into the restored
        world. Exported domain events can be written back in the same
        transaction so the debug log survives the restore.
        """
        with self._lock, self._conn:
            if clear_history:
                self._conn.execute(
                    "DELETE FROM action_requests WHERE campaign_id = ?",
                    (state.campaign_id,),
                )
                self._conn.execute(
                    "DELETE FROM domain_events WHERE campaign_id = ?",
                    (state.campaign_id,),
                )
                self._conn.execute(
                    "DELETE FROM campaign_snapshots WHERE campaign_id = ?",
                    (state.campaign_id,),
                )
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
            for event in events or []:
                if not isinstance(event, dict) or not event.get("event_type"):
                    continue
                try:
                    event_version = int(
                        event["state_version"]
                        if event.get("state_version") is not None
                        else state.version
                    )
                except (TypeError, ValueError):
                    event_version = state.version
                self._conn.execute(
                    "INSERT OR REPLACE INTO domain_events"
                    " (id, campaign_id, request_id, event_type, payload_json,"
                    "  state_version, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.get("id") or str(uuid.uuid4()),
                        state.campaign_id,
                        event.get("request_id") or "",
                        str(event["event_type"]),
                        json.dumps(event.get("payload") or {}, sort_keys=True),
                        event_version,
                        event.get("created_at") or _now(),
                    ),
                )

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
        expected_version: Optional[int] = None,
    ) -> None:
        """Commit the request row, the new state and the domain events atomically.

        The request row is inserted first: a duplicate request_id raises
        sqlite3.IntegrityError and the whole transaction (including the state
        update) rolls back.

        ``expected_version`` is the version the caller read before deciding the
        outcome. When given, the state row is only updated if it still holds
        that version; otherwise StateConflictError is raised and the whole
        transaction (request row included) rolls back.
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
            # Compare-and-set: the row is only written when the caller's
            # expected version still matches, so a slower concurrent request
            # cannot silently overwrite a faster one (lost update). Callers
            # that pass no expected version keep the pre-existing behaviour.
            if expected_version is None:
                updated = self._conn.execute(
                    "UPDATE campaign_state SET version = ?, state_json = ?, updated_at = ?"
                    " WHERE campaign_id = ?",
                    (state.version, state.model_dump_json(), _now(), campaign_id),
                )
            else:
                updated = self._conn.execute(
                    "UPDATE campaign_state SET version = ?, state_json = ?, updated_at = ?"
                    " WHERE campaign_id = ? AND version = ?",
                    (
                        state.version,
                        state.model_dump_json(),
                        _now(),
                        campaign_id,
                        expected_version,
                    ),
                )
            if updated.rowcount == 0:
                self._raise_missing_or_conflict(campaign_id, expected_version)
            self._insert_snapshot(state, request_id=request_id)
            self.prune_snapshots(campaign_id)
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

    def _raise_missing_or_conflict(self, campaign_id: str, expected: Optional[int]) -> None:
        row = self._conn.execute(
            "SELECT version FROM campaign_state WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"campaign '{campaign_id}' has no bound state; bind a world first"
            )
        raise StateConflictError(
            campaign_id,
            expected_version=expected if expected is not None else -1,
            actual_version=int(row["version"]),
        )

    def _history_belongs_to_another_world(
        self, campaign_id: str, versions: List[int]
    ) -> bool:
        """A rebound or locally re-edited world must not lose its old history."""
        current = self._conn.execute(
            "SELECT world_id, state_json FROM campaign_state WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if current is None:
            return False
        current_world_id = current["world_id"] or ""
        if not current_world_id:
            return False
        anchor = self._conn.execute(
            "SELECT state_json FROM campaign_snapshots"
            " WHERE campaign_id = ? AND version = 0",
            (campaign_id,),
        ).fetchone()
        if anchor is not None:
            # Version 0 is always kept, so it is the authority on which world
            # this history describes.
            return _worlds_disagree(anchor["state_json"], current_world_id)
        try:
            live_world_id = json.loads(current["state_json"]).get("world_id") or ""
        except (TypeError, ValueError):
            return False
        return bool(live_world_id) and live_world_id != current_world_id

    def prune_snapshots(self, campaign_id: str) -> int:
        """Drop low-value snapshots; keep every live state and the anchors.

        Called after a commit, inside the same transaction. Returns the number
        of rows deleted. Rollback and state-diff read snapshots, so the newest
        versions and version 0..SNAPSHOT_KEEP_OLDEST-1 are never removed.
        """
        rows = self._conn.execute(
            "SELECT version FROM campaign_snapshots"
            " WHERE campaign_id = ? ORDER BY version DESC",
            (campaign_id,),
        ).fetchall()
        versions = [int(row["version"]) for row in rows]
        if len(versions) <= SNAPSHOT_KEEP_RECENT + SNAPSHOT_KEEP_OLDEST:
            return 0
        keep = set(versions[:SNAPSHOT_KEEP_RECENT])
        keep.update(v for v in versions if v < SNAPSHOT_KEEP_OLDEST)
        oldest_kept = max(keep) if keep else 0
        for version in versions:
            if version in keep:
                continue
            if version > oldest_kept and version % SNAPSHOT_KEEP_EVERY == 0:
                keep.add(version)
        dropped = [v for v in versions if v not in keep]
        if not dropped:
            return 0
        if self._history_belongs_to_another_world(campaign_id, versions):
            # A local edit or a rebind changed the world under this campaign.
            # Keep the past readable instead of rewriting it.
            logger.warning(
                "campaign %s: snapshots describe a different world; keeping %d rows",
                campaign_id,
                len(versions),
            )
            return 0
        self._conn.executemany(
            "DELETE FROM campaign_snapshots WHERE campaign_id = ? AND version = ?",
            [(campaign_id, version) for version in dropped],
        )
        for version in dropped:
            self._conn.execute(
                "INSERT INTO domain_events"
                " (id, campaign_id, request_id, event_type, payload_json,"
                "  state_version, created_at)"
                " VALUES (?, ?, '', 'snapshot.pruned', ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    campaign_id,
                    json.dumps({"version": version}, sort_keys=True),
                    version,
                    _now(),
                ),
            )
        return len(dropped)

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
            "SELECT version, request_id, created_at, LENGTH(state_json) AS bytes"
            " FROM campaign_snapshots"
            " WHERE campaign_id = ? ORDER BY version DESC LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        entries: List[Dict[str, Any]] = []
        stored_bytes = 0
        for row in rows:
            size = int(row["bytes"] or 0)
            stored_bytes += size
            entries.append(
                {
                    "version": row["version"],
                    "request_id": row["request_id"],
                    "created_at": row["created_at"],
                    "bytes": size,
                    "stored_bytes": stored_bytes,
                }
            )
        return entries

    def snapshot_stats(self, campaign_id: str) -> Dict[str, Any]:
        """How many snapshots a campaign keeps and what one copy costs."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(state_json)), 0) AS bytes"
            " FROM campaign_snapshots WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        count = int(row["count"] or 0)
        total = int(row["bytes"] or 0)
        return {
            "campaign_id": campaign_id,
            "count": count,
            "total_bytes": total,
            "average_bytes": (total // count) if count else 0,
        }

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

    def narrative_history(
        self, campaign_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Project durable requests and prose once per turn, oldest first.

        `limit` counts recent requests, including rejections, not domain events.
        This view never reads legacy mirrors or changes stored records.
        """
        rows = self._conn.execute(
            "SELECT r.request_id, r.request_json, r.result_json, r.created_at,"
            " (SELECT e.payload_json FROM domain_events e"
            "  WHERE e.campaign_id = r.campaign_id AND e.request_id = r.request_id"
            "  AND e.event_type = 'narrator.response' ORDER BY e.rowid DESC LIMIT 1) AS prose"
            " FROM action_requests r WHERE r.campaign_id = ?"
            " AND r.status IN ('applied', 'rejected') ORDER BY r.rowid DESC LIMIT ?",
            (campaign_id, -1 if limit is None else max(0, limit)),
        ).fetchall()

        def object_payload(raw):
            try:
                value = json.loads(raw or "{}")
            except (TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

        def text_value(value):
            return value if isinstance(value, str) and value.strip() else ""

        records: List[Dict[str, Any]] = []
        for row in reversed(rows):
            request = object_payload(row["request_json"])
            result = object_payload(row["result_json"])
            params = request.get("params")
            params = params if isinstance(params, dict) else {}
            player = text_value(params.get("text")) or text_value(request.get("text"))
            if not player:
                player = " ".join(filter(None, (
                    text_value(request.get("action")),
                    text_value(request.get("target_id")) or text_value(params.get("action_id")),
                )))
            narrative = result.get("narrative")
            narrative = narrative if isinstance(narrative, dict) else {}
            prose = text_value(object_payload(row["prose"]).get("text"))
            if not prose and narrative.get("status") == "ok":
                prose = text_value(narrative.get("text"))
            prose = prose or text_value(result.get("message"))
            for kind, text in (("PLAYER_ACTION", player), ("NARRATOR_RESPONSE", prose)):
                if text:
                    records.append({
                        "campaign_id": campaign_id, "request_id": row["request_id"],
                        "event_type": kind, "payload": {"text": text},
                        "created_at": row["created_at"],
                    })
        return records

    # ---------------------------------------------------------------- events
    def list_events(
        self, campaign_id: str, limit: int = 50, after_version: int = 0
    ) -> List[Dict[str, Any]]:
        housekeeping = ", ".join(f"'{name}'" for name in _HOUSEKEEPING_EVENTS)
        rows = self._conn.execute(
            "SELECT * FROM domain_events"
            " WHERE campaign_id = ? AND state_version > ?"
            f" AND event_type NOT IN ({housekeeping})"
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