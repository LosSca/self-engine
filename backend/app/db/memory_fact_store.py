"""Persistent store for structured memory facts, their evidence and entities.

Deliberately a separate SQLite file from events.db/scenarios.db: facts are a
new layer derived from events, and putting them in their own file keeps the
existing schema versions untouched (no ALTER on tables the rest of the app
already migrates) while making the whole layer droppable - delete memory.db
and the game still runs, it just loses verified facts.

The store is a dumb persistence layer. Policy - which evidence type may reach
ACTIVE, when a fact supersedes another, how confidence aggregates - lives in
app.models.memory and app.services.memory_ingestion, so that it can be tested
without a database.
"""
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Iterable, Optional, Sequence

from app.models.memory import (
    FactStatus,
    LIVE_STATUSES,
    MemoryEntity,
    MemoryEvidence,
    MemoryFact,
    MemoryRelation,
    can_transition,
    normalize_alias,
    transition_path,
)

#: Facts in these states describe the world right now. Used by retrieval when
#: no explicit status filter is given: EXTRACTED/CANDIDATE rows are proposals,
#: SUPERSEDED rows are history, and neither should answer "what is true now".
DEFAULT_READ_STATUSES = tuple(sorted(LIVE_STATUSES))

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Table order matters for deletes (FK-free schema, but logical dependencies).
_TABLES = (
    "memory_evidence",
    "memory_relations",
    "memory_facts",
    "memory_entities",
    "memory_ingest_cursor",
)


def default_db_path() -> str:
    """Path of the fact store, overridable so tests never touch the real file."""
    return os.environ.get("MEMORY_DB_PATH", os.path.join(_BACKEND_DIR, "memory.db"))


def fact_fingerprint(
    campaign_id: str,
    subject_id: str,
    predicate: str,
    object_value: str,
    valid_from: Optional[str] = None,
) -> str:
    """Stable identity of a fact's *content*.

    Two extractions that produced the same claim for the same validity window
    are the same fact with more evidence, not two facts. The window is part of
    the key because "Eileen is an enemy (ch.2)" and "Eileen is a friend (ch.3)"
    are different facts that must coexist.
    """
    material = "|".join(
        [
            campaign_id.strip(),
            normalize_alias(subject_id),
            normalize_alias(predicate),
            normalize_alias(object_value),
            (valid_from or "").strip(),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.utcnow().isoformat()


def _json_list(value) -> str:
    return json.dumps(list(value or []))


def _load_list(raw) -> list:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


class MemoryFactStore:
    """SQLite-backed store for facts, evidence, entities and relations.

    Concurrency follows the rest of the project: one connection guarded by a
    lock, opened with check_same_thread=False because FastAPI handlers and the
    background consolidation task share the instance.
    """

    SCHEMA_VERSION = 1

    _MIGRATIONS = {
        1: [
            """CREATE TABLE IF NOT EXISTS memory_facts (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_value TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                importance REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'extracted',
                valid_from TEXT,
                valid_until TEXT,
                recorded_at TEXT NOT NULL,
                source_event_ids TEXT NOT NULL DEFAULT '[]',
                observer_ids TEXT NOT NULL DEFAULT '[]',
                supersedes_fact_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_fingerprint ON memory_facts(campaign_id, fingerprint)",
            "CREATE INDEX IF NOT EXISTS idx_fact_subject ON memory_facts(campaign_id, subject_id, predicate)",
            "CREATE INDEX IF NOT EXISTS idx_fact_status ON memory_facts(campaign_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_fact_window ON memory_facts(campaign_id, valid_from, valid_until)",
            """CREATE TABLE IF NOT EXISTS memory_evidence (
                id TEXT PRIMARY KEY,
                fact_id TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL DEFAULT '',
                evidence_type TEXT NOT NULL,
                observer_id TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.5,
                recorded_at TEXT NOT NULL,
                evidence_content TEXT NOT NULL DEFAULT ''
            )""",
            # Re-running extraction over the same event must not double the
            # evidence count: one event contributes at most one evidence row
            # per (fact, type, observer).
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_unique
               ON memory_evidence(fact_id, source_event_id, evidence_type, observer_id)""",
            "CREATE INDEX IF NOT EXISTS idx_evidence_event ON memory_evidence(campaign_id, source_event_id)",
            """CREATE TABLE IF NOT EXISTS memory_entities (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                name TEXT NOT NULL,
                normalized TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'unknown',
                aliases TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_name ON memory_entities(campaign_id, normalized)",
            """CREATE TABLE IF NOT EXISTS memory_relations (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                from_entity_id TEXT NOT NULL,
                to_entity_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                valid_from TEXT,
                valid_until TEXT,
                source_fact_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_relation_from ON memory_relations(campaign_id, from_entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_relation_to ON memory_relations(campaign_id, to_entity_id)",
            """CREATE TABLE IF NOT EXISTS memory_ingest_cursor (
                campaign_id TEXT PRIMARY KEY,
                last_event_created_at TEXT NOT NULL DEFAULT '',
                last_event_id TEXT NOT NULL DEFAULT '',
                processed_events INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""",
        ],
    }

    def __init__(self, db_path: Optional[str] = None):
        path = db_path or default_db_path()
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()

    # -- schema ------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """The schema this database is at (0 for a file that is not one of ours)."""
        return self._get_schema_version()

    @property
    def db_path(self) -> str:
        """Where this store lives, so a migration can report and back up."""
        row = self._conn.execute("PRAGMA database_list").fetchone()
        return str(row[2]) if row else ""

    def _get_schema_version(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return 0

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = self._get_schema_version()
            for version in sorted(self._MIGRATIONS):
                if version <= current:
                    continue
                for sql in self._MIGRATIONS[version]:
                    self._conn.execute(sql)
                self._conn.execute(
                    "INSERT INTO schema_version VALUES (?, ?)", (version, _now())
                )
            self._conn.commit()

    # -- facts -------------------------------------------------------

    def add_fact(self, fact: MemoryFact, *, fingerprint: Optional[str] = None):
        """Insert a fact, or return the existing one with the same content.

        Returns `(fact, created)`. Idempotent on the content fingerprint, so
        a retried ingestion run converges instead of accumulating duplicates.
        """
        key = fingerprint or fact_fingerprint(
            fact.campaign_id, fact.subject_id, fact.predicate, fact.object_value, fact.valid_from
        )
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM memory_facts WHERE campaign_id=? AND fingerprint=?",
                (fact.campaign_id, key),
            ).fetchone()
            if existing is not None:
                return self._row_to_fact(existing), False

            fact_id = fact.id or f"fact_{uuid.uuid4().hex[:16]}"
            now = _now()
            with self._conn:
                self._conn.execute(
                    """INSERT INTO memory_facts (
                        id, campaign_id, fingerprint, subject_id, predicate, object_value,
                        memory_type, evidence_type, confidence, importance, status,
                        valid_from, valid_until, recorded_at, source_event_ids,
                        observer_ids, supersedes_fact_id, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        fact_id,
                        fact.campaign_id,
                        key,
                        fact.subject_id,
                        fact.predicate,
                        fact.object_value,
                        str(fact.memory_type),
                        str(fact.evidence_type),
                        float(fact.confidence),
                        float(fact.importance),
                        str(fact.status),
                        fact.valid_from,
                        fact.valid_until,
                        fact.recorded_at or now,
                        _json_list(fact.source_event_ids),
                        _json_list(fact.observer_ids),
                        fact.supersedes_fact_id,
                        now,
                        now,
                    ),
                )
            stored = self.get_fact(fact_id)
            assert stored is not None  # just inserted under the same lock
            return stored, True

    def get_fact(self, fact_id: str):
        row = self._conn.execute(
            "SELECT * FROM memory_facts WHERE id=?", (fact_id,)
        ).fetchone()
        return self._row_to_fact(row) if row else None

    def find_facts(
        self,
        campaign_id: str,
        *,
        subject_id=None,
        predicate=None,
        memory_type=None,
        statuses=DEFAULT_READ_STATUSES,
        at=None,
        limit: int = 200,
    ):
        """Facts matching a filter, newest validity window first.

        `statuses` defaults to what a reader may believe right now. Pass an
        explicit tuple for a narrowed view, or `None` for "every row the store
        holds", which is what provenance and audit views need.

        `at` restricts to facts valid at that instant, which is what makes a
        "what did she think of me in chapter two" query answerable.
        """
        query = "SELECT * FROM memory_facts WHERE campaign_id=?"
        params = [campaign_id]
        if subject_id is not None:
            query += " AND subject_id=?"
            params.append(subject_id)
        if predicate is not None:
            query += " AND predicate=?"
            params.append(predicate)
        if memory_type is not None:
            query += " AND memory_type=?"
            params.append(str(memory_type))
        if at is not None:
            # A historical query asks what was true THEN, so the status is the
            # status the fact has *now*, not what it was: a superseded fact is
            # still the correct answer for the chapter it was valid in. Only
            # REJECTED and RETRACTED facts are withheld, because those mean the
            # claim was never true or was withdrawn outright.
            query += " AND status NOT IN (?, ?)"
            params.extend([FactStatus.REJECTED.value, FactStatus.RETRACTED.value])
        else:
            wanted = [str(s) for s in statuses] if statuses is not None else []
            if wanted:
                query += " AND status IN (" + ",".join("?" for _ in wanted) + ")"
                params.extend(wanted)
        if at is not None:
            query += " AND (valid_from IS NULL OR valid_from <= ?)"
            params.append(at)
            query += " AND (valid_until IS NULL OR valid_until > ?)"
            params.append(at)
        query += " ORDER BY COALESCE(valid_from, created_at) DESC, created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def update_status(
        self,
        fact_id: str,
        status,
        *,
        supersedes_fact_id=None,
        valid_until=None,
        confidence=None,
        importance=None,
    ):
        """Move a fact through the state machine.

        Illegal transitions raise ValueError rather than writing a row the
        rest of the system would then have to defend against.
        """
        # use_enum_values makes the model carry bare strings, while a caller
        # naturally passes FactStatus.X. Normalise once here: a fact that is
        # refused an active -> superseded move because of a repr is a bug that
        # looks exactly like a policy decision.
        target = str(getattr(status, "value", status))
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_facts WHERE id=?", (fact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown fact: {fact_id}")
            current = row["status"]
            # A recorded fact moves one hop at a time (models.memory
            # ALLOWED_TRANSITIONS). Multi-step routes such as
            # extracted -> active exist only so the pipeline can walk them
            # hop by hop; letting update_status jump them would make the
            # "a candidate must be verified before it is believed"
            # guarantee unenforceable.
            if current != target and not can_transition(current, target):
                raise ValueError(f"illegal fact transition: {current} -> {target}")

            sets = ["status=?", "updated_at=?"]
            params = [target, _now()]
            if supersedes_fact_id is not None:
                sets.append("supersedes_fact_id=?")
                params.append(supersedes_fact_id)
            if valid_until is not None:
                sets.append("valid_until=?")
                params.append(valid_until)
            if confidence is not None:
                sets.append("confidence=?")
                params.append(max(0.0, min(1.0, float(confidence))))
            if importance is not None:
                sets.append("importance=?")
                params.append(max(0.0, min(1.0, float(importance))))
            params.append(fact_id)
            with self._conn:
                self._conn.execute(
                    "UPDATE memory_facts SET " + ", ".join(sets) + " WHERE id=?", tuple(params)
                )
        stored = self.get_fact(fact_id)
        assert stored is not None
        return stored

    def advance_status(self, fact_id: str, status, **kwargs):
        """Move a fact to a later state along a legal path, in one write.

        Unlike :meth:`update_status`, which only permits a single hop, this
        accepts a target reachable through intermediate states (a settled
        engine fact walks EXTRACTED -> CANDIDATE -> VERIFIED -> ACTIVE). The
        path is validated before anything is written, so an unreachable state
        raises ValueError instead of leaving a half-moved row.
        """
        target = str(getattr(status, "value", status))
        current = self.get_fact(fact_id)
        if current is None:
            raise KeyError(f"unknown fact: {fact_id}")
        if target == current.status:
            return current
        if target in (FactStatus.REJECTED.value, FactStatus.RETRACTED.value):
            raise ValueError(
                "advance_status refuses terminal states; use update_status explicitly"
            )
        path = transition_path(current.status, target)
        if not path:
            raise ValueError(f"no legal path: {current.status} -> {target}")
        # Walk the route hop by hop so every hop passes the same single-hop
        # guard a hand-written caller faces, and so a fact that is read
        # between the steps is never sitting in a state it could not reach.
        kwargs = dict(kwargs)
        for hop in path[1:]:
            extra = kwargs if hop == path[-1] else {}
            self.update_status(fact_id, hop, **extra)
        stored = self.get_fact(fact_id)
        assert stored is not None
        return stored

    def supersede(self, old_fact_id: str, new_fact_id: str, *, ended_at=None):
        """Close an old fact's validity window because a new one replaced it.

        The old row is kept (never deleted): "Eileen was my enemy" stays
        queryable as history, it just stops being the current answer.
        """
        new_fact = self.get_fact(new_fact_id)
        if new_fact is None:
            raise KeyError(f"unknown fact: {new_fact_id}")
        old_fact = self.get_fact(old_fact_id)
        if old_fact is None:
            raise KeyError(f"unknown fact: {old_fact_id}")
        boundary = ended_at or new_fact.valid_from or new_fact.recorded_at or _now()
        return self.update_status(
            old_fact_id, FactStatus.SUPERSEDED.value, valid_until=boundary
        )

    def link_supersedes(self, fact_id: str, previous_fact_id: str) -> None:
        """Record the replacement chain without changing either fact's status."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE memory_facts SET supersedes_fact_id=?, updated_at=? WHERE id=?",
                (previous_fact_id, _now(), fact_id),
            )

    def add_source_event(self, fact_id: str, event_id: str):
        """Append an event id to a fact's provenance, without duplicating it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT source_event_ids FROM memory_facts WHERE id=?", (fact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown fact: {fact_id}")
            ids = _load_list(row["source_event_ids"])
            if event_id and event_id not in ids:
                ids.append(event_id)
                with self._conn:
                    self._conn.execute(
                        "UPDATE memory_facts SET source_event_ids=?, updated_at=? WHERE id=?",
                        (_json_list(ids), _now(), fact_id),
                    )
        stored = self.get_fact(fact_id)
        assert stored is not None
        return stored

    def add_observers(self, fact_id: str, observers):
        """Record who witnessed a fact (drives per-NPC knowledge filtering)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT observer_ids FROM memory_facts WHERE id=?", (fact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown fact: {fact_id}")
            current = _load_list(row["observer_ids"])
            changed = False
            for name in observers:
                if name and name not in current:
                    current.append(name)
                    changed = True
            if changed:
                with self._conn:
                    self._conn.execute(
                        "UPDATE memory_facts SET observer_ids=?, updated_at=? WHERE id=?",
                        (_json_list(current), _now(), fact_id),
                    )
        stored = self.get_fact(fact_id)
        assert stored is not None
        return stored

    # -- evidence ----------------------------------------------------

    def add_evidence(self, evidence: MemoryEvidence):
        """Attach one piece of evidence to a fact. Idempotent per event."""
        evidence_id = evidence.id or f"ev_{uuid.uuid4().hex[:16]}"
        recorded = evidence.recorded_at or _now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """INSERT OR IGNORE INTO memory_evidence (
                    id, fact_id, campaign_id, source_event_id, evidence_type,
                    observer_id, confidence, recorded_at, evidence_content
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    evidence_id,
                    evidence.fact_id,
                    evidence.campaign_id,
                    evidence.source_event_id,
                    str(evidence.evidence_type),
                    evidence.observer_id,
                    float(evidence.confidence),
                    recorded,
                    evidence.evidence_content,
                ),
            )
            created = cursor.rowcount > 0
        if created:
            evidence.id = evidence_id
            evidence.recorded_at = recorded
            return evidence, True
        row = self._conn.execute(
            """SELECT * FROM memory_evidence
               WHERE fact_id=? AND source_event_id=? AND evidence_type=? AND observer_id=?""",
            (
                evidence.fact_id,
                evidence.source_event_id,
                str(evidence.evidence_type),
                evidence.observer_id,
            ),
        ).fetchone()
        return (self._row_to_evidence(row) if row else evidence), False

    def get_evidence(self, fact_id: str):
        rows = self._conn.execute(
            "SELECT * FROM memory_evidence WHERE fact_id=? ORDER BY recorded_at ASC",
            (fact_id,),
        ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def count_evidence(self, fact_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM memory_evidence WHERE fact_id=?", (fact_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def facts_from_event(self, campaign_id: str, event_id: str):
        rows = self._conn.execute(
            "SELECT DISTINCT fact_id FROM memory_evidence WHERE campaign_id=? AND source_event_id=?",
            (campaign_id, event_id),
        ).fetchall()
        return [row[0] for row in rows]

    # -- entities ----------------------------------------------------

    def upsert_entity(self, entity: MemoryEntity):
        """Insert an entity or merge new aliases into the existing row."""
        normalized = normalize_alias(entity.canonical_name)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_entities WHERE campaign_id=? AND normalized=?",
                (entity.campaign_id, normalized),
            ).fetchone()
            if row is None:
                entity_id = entity.id or f"ent_{uuid.uuid4().hex[:16]}"
                now = _now()
                with self._conn:
                    self._conn.execute(
                        """INSERT INTO memory_entities (
                            id, campaign_id, name, normalized, kind, aliases,
                            description, created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            entity_id,
                            entity.campaign_id,
                            entity.canonical_name,
                            normalized,
                            str(entity.entity_type),
                            _json_list(entity.aliases),
                            entity.description,
                            now,
                            now,
                        ),
                    )
                return MemoryEntity(
                    id=entity_id,
                    campaign_id=entity.campaign_id,
                    canonical_name=entity.canonical_name,
                    entity_type=entity.entity_type,
                    aliases=list(entity.aliases),
                    description=entity.description,
                    created_at=now,
                    updated_at=now,
                )

            aliases = _load_list(row["aliases"])
            changed = False
            for alias in entity.aliases:
                if alias and alias not in aliases and normalize_alias(alias) != normalized:
                    aliases.append(alias)
                    changed = True
            description = row["description"] or entity.description
            if changed or (entity.description and not row["description"]):
                with self._conn:
                    self._conn.execute(
                        "UPDATE memory_entities SET aliases=?, description=?, updated_at=? WHERE id=?",
                        (_json_list(aliases), description, _now(), row["id"]),
                    )
        return self.get_entity_by_id(row["id"])

    def get_entity_by_id(self, entity_id: str):
        row = self._conn.execute(
            "SELECT * FROM memory_entities WHERE id=?", (entity_id,)
        ).fetchone()
        return self._row_to_entity(row) if row else None

    def resolve_entity(self, campaign_id: str, name: str):
        """Find an entity by canonical name, alias or normalised form."""
        wanted = normalize_alias(name)
        if not wanted:
            return None
        row = self._conn.execute(
            "SELECT * FROM memory_entities WHERE campaign_id=? AND normalized=?",
            (campaign_id, wanted),
        ).fetchone()
        if row is not None:
            return self._row_to_entity(row)
        # Alias match: scan the (small) entity table rather than indexing JSON.
        rows = self._conn.execute(
            "SELECT * FROM memory_entities WHERE campaign_id=?", (campaign_id,)
        ).fetchall()
        for candidate in rows:
            entity = self._row_to_entity(candidate)
            if any(normalize_alias(alias) == wanted for alias in entity.aliases):
                return entity
        return None

    def list_entities(self, campaign_id: str, *, kind=None, limit: int = 500):
        query = "SELECT * FROM memory_entities WHERE campaign_id=?"
        params = [campaign_id]
        if kind is not None:
            query += " AND kind=?"
            params.append(str(kind))
        query += " ORDER BY name ASC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_entity(row) for row in rows]

    # -- relations ---------------------------------------------------

    def add_relation(self, relation: MemoryRelation):
        """Add a graph edge between two entities."""
        relation_id = relation.id or f"rel_{uuid.uuid4().hex[:16]}"
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO memory_relations (
                    id, campaign_id, from_entity_id, to_entity_id, predicate,
                    confidence, valid_from, valid_until, source_fact_ids,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    relation_id,
                    relation.campaign_id,
                    relation.subject_id,
                    relation.object_id,
                    relation.predicate,
                    float(relation.confidence),
                    relation.valid_from,
                    relation.valid_until,
                    _json_list(relation.source_event_ids),
                    now,
                    now,
                ),
            )
        return MemoryRelation(
            id=relation_id,
            campaign_id=relation.campaign_id,
            subject_id=relation.subject_id,
            predicate=relation.predicate,
            object_id=relation.object_id,
            confidence=relation.confidence,
            valid_from=relation.valid_from,
            valid_until=relation.valid_until,
            source_event_ids=list(relation.source_event_ids),
        )

    def get_relations(self, campaign_id: str, *, entity_id=None, at=None, limit: int = 200):
        query = "SELECT * FROM memory_relations WHERE campaign_id=?"
        params = [campaign_id]
        if entity_id is not None:
            query += " AND (from_entity_id=? OR to_entity_id=?)"
            params.extend([entity_id, entity_id])
        if at is not None:
            query += " AND (valid_from IS NULL OR valid_from <= ?) AND (valid_until IS NULL OR valid_until > ?)"
            params.extend([at, at])
        query += " ORDER BY confidence DESC, created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_relation(row) for row in rows]

    # -- ingestion cursor --------------------------------------------

    def get_cursor(self, campaign_id: str) -> dict:
        """How far the fact pipeline has consumed events for a campaign."""
        row = self._conn.execute(
            "SELECT * FROM memory_ingest_cursor WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            return {
                "campaign_id": campaign_id,
                "last_event_created_at": "",
                "last_event_id": "",
                "processed_events": 0,
            }
        return {
            "campaign_id": row["campaign_id"],
            "last_event_created_at": row["last_event_created_at"],
            "last_event_id": row["last_event_id"],
            "processed_events": int(row["processed_events"]),
        }

    def set_cursor(
        self,
        campaign_id: str,
        last_event_created_at: str,
        last_event_id: str,
        *,
        processed_events: int = 0,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO memory_ingest_cursor (
                    campaign_id, last_event_created_at, last_event_id,
                    processed_events, updated_at
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                    last_event_created_at=excluded.last_event_created_at,
                    last_event_id=excluded.last_event_id,
                    processed_events=memory_ingest_cursor.processed_events + excluded.processed_events,
                    updated_at=excluded.updated_at""",
                (campaign_id, last_event_created_at, last_event_id, int(processed_events), _now()),
            )

    # -- aggregates / maintenance ------------------------------------

    def overview(self, campaign_id: str) -> dict:
        """Counts per status and memory type - cheap health check for the UI."""
        by_status = {
            row[0]: int(row[1])
            for row in self._conn.execute(
                "SELECT status, COUNT(*) FROM memory_facts WHERE campaign_id=? GROUP BY status",
                (campaign_id,),
            ).fetchall()
        }
        by_type = {
            row[0]: int(row[1])
            for row in self._conn.execute(
                "SELECT memory_type, COUNT(*) FROM memory_facts WHERE campaign_id=? GROUP BY memory_type",
                (campaign_id,),
            ).fetchall()
        }
        evidence = self._conn.execute(
            "SELECT COUNT(*) FROM memory_evidence WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        entities = self._conn.execute(
            "SELECT COUNT(*) FROM memory_entities WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        relations = self._conn.execute(
            "SELECT COUNT(*) FROM memory_relations WHERE campaign_id=?", (campaign_id,)
        ).fetchone()
        return {
            "campaign_id": campaign_id,
            "facts": sum(by_status.values()),
            "by_status": by_status,
            "by_type": by_type,
            "evidence": int(evidence[0]) if evidence else 0,
            "entities": int(entities[0]) if entities else 0,
            "relations": int(relations[0]) if relations else 0,
            "cursor": self.get_cursor(campaign_id),
        }

    def delete_by_campaign(self, campaign_id: str) -> int:
        """Remove everything this store holds for one campaign.

        Only used by tests and by an explicit "forget this campaign" action.
        Events are never touched: history lives in events.db.
        """
        removed = 0
        with self._lock, self._conn:
            for table in _TABLES:
                cursor = self._conn.execute(
                    "DELETE FROM " + table + " WHERE campaign_id=?", (campaign_id,)
                )
                removed += cursor.rowcount
        return removed

    # -- row mapping -------------------------------------------------

    @staticmethod
    def _row_to_fact(row):
        return MemoryFact(
            id=row["id"],
            campaign_id=row["campaign_id"],
            subject_id=row["subject_id"],
            predicate=row["predicate"],
            object_value=row["object_value"],
            memory_type=row["memory_type"],
            evidence_type=row["evidence_type"],
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            status=row["status"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            recorded_at=row["recorded_at"],
            source_event_ids=_load_list(row["source_event_ids"]),
            observer_ids=_load_list(row["observer_ids"]),
            supersedes_fact_id=row["supersedes_fact_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_evidence(row):
        return MemoryEvidence(
            id=row["id"],
            fact_id=row["fact_id"],
            campaign_id=row["campaign_id"],
            source_event_id=row["source_event_id"],
            evidence_type=row["evidence_type"],
            observer_id=row["observer_id"],
            confidence=float(row["confidence"]),
            recorded_at=row["recorded_at"],
            evidence_content=row["evidence_content"],
        )

    @staticmethod
    def _row_to_entity(row):
        return MemoryEntity(
            id=row["id"],
            campaign_id=row["campaign_id"],
            canonical_name=row["name"],
            entity_type=row["kind"],
            aliases=_load_list(row["aliases"]),
            description=row["description"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_relation(row):
        return MemoryRelation(
            id=row["id"],
            campaign_id=row["campaign_id"],
            subject_id=row["from_entity_id"],
            predicate=row["predicate"],
            object_id=row["to_entity_id"],
            confidence=float(row["confidence"]),
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
            source_event_ids=_load_list(row["source_fact_ids"]),
        )

    # -- lifecycle ---------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()
