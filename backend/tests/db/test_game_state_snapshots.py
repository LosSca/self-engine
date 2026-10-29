"""Snapshot retention: bounded history without losing rollback targets."""
from app.db import game_state_store as store_module
from app.db.game_state_store import GameStateStore
from app.models.game import GameState

from tests.support import bound_world


def _bind(store):
    bound_world(store)


def _commit_versions(store, start, stop):
    base = store.get_state("camp-1")
    for version in range(start, stop):
        state = GameState(
            campaign_id="camp-1",
            world_id=base.world_id,
            location_id=base.location_id,
            version=version,
        )
        store.commit_action(
            campaign_id="camp-1",
            request_id=f"req-{version}",
            request_payload={"action": "look"},
            status="applied",
            result_payload={"status": "applied"},
            state=state,
            events=[],
        )


def _patch(monkeypatch, recent, oldest, every):
    monkeypatch.setattr(store_module, "SNAPSHOT_KEEP_RECENT", recent)
    monkeypatch.setattr(store_module, "SNAPSHOT_KEEP_OLDEST", oldest)
    monkeypatch.setattr(store_module, "SNAPSHOT_KEEP_EVERY", every)


def test_recent_snapshots_are_never_pruned(tmp_path, monkeypatch):
    _patch(monkeypatch, 5, 2, 3)
    store = GameStateStore(str(tmp_path / "s.db"))
    try:
        _bind(store)
        _commit_versions(store, 1, 31)
        versions = [row["version"] for row in store.list_snapshots("camp-1", limit=200)]
        for version in range(26, 31):
            assert version in versions, f"recent version {version} was pruned"
        assert 0 in versions, "the bound state must stay restorable"
    finally:
        store.close()


def test_history_is_bounded(tmp_path, monkeypatch):
    _patch(monkeypatch, 2, 1, 4)
    store = GameStateStore(str(tmp_path / "s.db"))
    try:
        _bind(store)
        _commit_versions(store, 1, 21)
        assert store.get_state("camp-1").version == 20
        versions = [row["version"] for row in store.list_snapshots("camp-1", limit=200)]
        assert len(versions) < 21, "history must be bounded"
        assert 20 in versions and 0 in versions
        assert store.prune_snapshots("camp-1") == 0, "the commit path already pruned"
    finally:
        store.close()


def test_pruned_versions_are_recorded_off_the_game_feed(tmp_path, monkeypatch):
    _patch(monkeypatch, 2, 1, 4)
    store = GameStateStore(str(tmp_path / "s.db"))
    try:
        _bind(store)
        _commit_versions(store, 1, 21)
        feed = store.list_events("camp-1", limit=500)
        assert all(event["event_type"] != "snapshot.pruned" for event in feed)
        rows = store._conn.execute(
            "SELECT COUNT(*) FROM domain_events WHERE event_type = 'snapshot.pruned'"
        ).fetchone()[0]
        assert rows > 0, "pruning must be auditable"
    finally:
        store.close()


def test_kept_snapshots_still_restore(tmp_path, monkeypatch):
    _patch(monkeypatch, 3, 1, 5)
    store = GameStateStore(str(tmp_path / "s.db"))
    try:
        _bind(store)
        _commit_versions(store, 1, 12)
        latest = store.get_snapshot("camp-1", 11)
        assert latest is not None and latest.version == 11
    finally:
        store.close()
