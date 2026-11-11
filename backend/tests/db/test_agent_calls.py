"""Phase 1/2: agent calls are auditable for one campaign."""
from __future__ import annotations

import pytest

from app.db.game_state_store import GameStateStore


@pytest.fixture()
def store(tmp_path):
    with GameStateStore(str(tmp_path / "state.db")) as handle:
        yield handle


def test_agent_calls_table_exists_after_migration(store):
    assert store._get_schema_version() >= 3


def test_record_and_list_round_trip(store):
    store.record_agent_call(
        role="narrative", task="narrate", provider="openai", model="m",
        status="ok", input_tokens=10, output_tokens=5, elapsed_ms=42,
        campaign_id="c1",
    )
    rows = store.list_agent_calls("c1")
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "narrative"
    assert row["model"] == "m"
    assert row["elapsed_ms"] == 42
    assert row["degraded"] in (0, False)


def test_calls_are_scoped_to_their_campaign(store):
    store.record_agent_call(role="narrative", campaign_id="c1")
    store.record_agent_call(role="system", campaign_id="c2")
    assert [r["role"] for r in store.list_agent_calls("c1")] == ["narrative"]
    assert [r["role"] for r in store.list_agent_calls("c2")] == ["system"]
    assert len(store.list_agent_calls()) == 2


def test_role_filter_and_limit(store):
    for role in ("narrative", "system", "narrative"):
        store.record_agent_call(role=role, campaign_id="c1")
    assert len(store.list_agent_calls("c1", role="narrative")) == 2
    assert len(store.list_agent_calls("c1", limit=1)) == 1


def test_failures_are_recorded_with_their_error(store):
    store.record_agent_call(
        role="system", status="error", error="RuntimeError: boom",
        degraded=True, campaign_id="c1",
    )
    row = store.list_agent_calls("c1")[0]
    assert row["status"] == "error"
    assert "boom" in row["error"]
    assert row["degraded"] in (1, True)


def test_an_unexpected_field_never_raises(store):
    # Unknown keywords must not be able to fail a game turn over telemetry.
    store.record_agent_call(role="narrative", campaign_id="c1", nonsense=1)
    rows = store.list_agent_calls("c1")
    assert [row["role"] for row in rows] == ["narrative"]
