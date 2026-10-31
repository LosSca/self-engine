"""The one-way PostgreSQL exporter translates schema and data safely."""
import importlib.util
import sqlite3
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "sqlite_to_postgres",
    Path(__file__).resolve().parents[2] / "scripts" / "sqlite_to_postgres.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_sql = _MODULE.build_sql


def test_exporter_qualifies_tables_and_escapes_values(tmp_path):
    source = tmp_path / "scenarios.db"
    connection = sqlite3.connect(source)
    connection.execute(
        "CREATE TABLE scenarios (id TEXT PRIMARY KEY, title TEXT NOT NULL, payload BLOB)"
    )
    connection.execute(
        "CREATE TABLE story_cards (id TEXT PRIMARY KEY, scenario_id TEXT REFERENCES scenarios(id))"
    )
    connection.execute(
        "INSERT INTO scenarios VALUES (?, ?, ?)",
        ("s1", "O'Hara", b"\x01\x02"),
    )
    connection.commit()
    connection.close()

    sql = build_sql([("scenarios", source)])
    assert 'CREATE SCHEMA IF NOT EXISTS "scenarios";' in sql
    assert "CREATE TABLE scenarios.scenarios" in sql
    assert "REFERENCES scenarios.scenarios(" in sql
    assert "O''Hara" in sql
    assert "0102" in sql
    assert "BYTEA" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql


def test_exporter_skips_missing_sources_without_failing(tmp_path):
    sql = build_sql([("missing", tmp_path / "nope.db")])
    assert "skipped missing source" in sql
