import pytest
from httpx import AsyncClient, ASGITransport
from app.engines.journal_engine import JournalEntry, JournalCategory
from datetime import datetime


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Imported here, not at module scope: routes_game opens its SQLite stores
    # on import, and doing that before the paths are patched leaves the rest of
    # the suite writing into this checkout.
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv("LLM_TRACE_DB_PATH", str(tmp_path / "traces.db"))
    from app.main import app as application

    return application


@pytest.fixture
def _journal():
    from app.api.routes_game import _journal as journal

    return journal


@pytest.mark.asyncio
async def test_get_journal_empty(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/game/no-such-campaign/journal")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_get_journal_with_entries(app, _journal):
    _journal._journals["test-j1"] = [
        JournalEntry("test-j1", JournalCategory.DISCOVERY, "Found a cave", datetime.utcnow().isoformat()),
        JournalEntry("test-j1", JournalCategory.COMBAT, "Fought a troll", datetime.utcnow().isoformat()),
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/game/test-j1/journal")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert data[0]["category"] == "DISCOVERY"
    assert data[1]["category"] == "COMBAT"
    del _journal._journals["test-j1"]


@pytest.mark.asyncio
async def test_get_journal_filter_category(app, _journal):
    _journal._journals["test-j2"] = [
        JournalEntry("test-j2", JournalCategory.DISCOVERY, "Found cave", datetime.utcnow().isoformat()),
        JournalEntry("test-j2", JournalCategory.COMBAT, "Fought troll", datetime.utcnow().isoformat()),
        JournalEntry("test-j2", JournalCategory.DISCOVERY, "Found map", datetime.utcnow().isoformat()),
    ]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/game/test-j2/journal?category=DISCOVERY")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert all(e["category"] == "DISCOVERY" for e in data)
    del _journal._journals["test-j2"]
