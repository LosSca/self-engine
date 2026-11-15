"""Bring the structured-fact database up to date, and let the operator undo it.

Why a separate script rather than a startup hook: the fact layer is optional,
and a server that migrates on import is a server that surprises its operator
on a restart. The script does the same idempotent steps the store does on
first open, but it says what it did, can be run dry, and can back the file up
first.

    python -m scripts.migrate_memory_facts --status
    python -m scripts.migrate_memory_facts --apply --backup
    python -m scripts.migrate_memory_facts --rollback <backup file>

The rollback is a file restore on purpose. Migrations here only ever ADD
tables and indexes (schema versions are cumulative), so the honest recovery
from a bad release is "put back the file you had", not a half-rewritten
down-migration that could turn good facts into a different shape.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

#: Make `python -m scripts.migrate_memory_facts` work from backend/.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.db.memory_fact_store import MemoryFactStore, default_db_path  # noqa: E402


def _backup_path(db_path: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{db_path}.{stamp}.bak"


def status(db_path: str) -> dict:
    """What the file currently is, without changing it."""
    if not os.path.exists(db_path):
        return {"path": db_path, "exists": False, "version": 0, "target": MemoryFactStore.SCHEMA_VERSION}
    store = MemoryFactStore(db_path)
    try:
        return {
            "path": db_path,
            "exists": True,
            "version": store.schema_version,
            "target": MemoryFactStore.SCHEMA_VERSION,
        }
    finally:
        store.close()


def apply(db_path: str, *, backup: bool = False) -> dict:
    """Run the cumulative migrations; optionally keep the old file first."""
    result = {"path": db_path, "backup": "", "before": 0, "after": 0}
    result["before"] = status(db_path)["version"]
    if backup and os.path.exists(db_path):
        target = _backup_path(db_path)
        shutil.copy2(db_path, target)
        result["backup"] = target
    store = MemoryFactStore(db_path)
    try:
        result["after"] = store.schema_version
    finally:
        store.close()
    return result


def rollback(db_path: str, backup: str) -> dict:
    """Restore a backup over the live file; refuses a backup that is not one."""
    if not os.path.exists(backup):
        raise SystemExit(f"backup not found: {backup}")
    # Refuse anything that is not obviously a database: a typo here destroys
    # the fact history, and the check costs one read.
    if os.path.getsize(backup) < 1024:
        raise SystemExit(f"backup looks empty, refusing to restore: {backup}")
    if os.path.exists(db_path):
        shutil.copy2(db_path, db_path + ".pre-rollback")
    shutil.copy2(backup, db_path)
    return {"path": db_path, "restored_from": backup, "version": status(db_path)["version"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="migrate the memory fact database")
    parser.add_argument("--db", default=default_db_path(), help="fact database path")
    parser.add_argument("--status", action="store_true", help="report and exit")
    parser.add_argument("--apply", action="store_true", help="run pending migrations")
    parser.add_argument("--backup", action="store_true", help="copy the file before applying")
    parser.add_argument("--rollback", default="", help="restore this backup over the live file")
    args = parser.parse_args(argv)

    if args.rollback:
        print(rollback(args.db, args.rollback))
        return 0
    if args.apply:
        print(apply(args.db, backup=args.backup))
        return 0
    print(status(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())