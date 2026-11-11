"""Phase 5: a player leaving stops the world; coming back does not catch up."""
from __future__ import annotations

import asyncio

import pytest


class FakeSession:
    """Only what the routes touch: paused state and the pending-task set."""

    def __init__(self):
        self.paused = False
        self._pending_tasks = set()
        self._pending_auto_plot_chunks = []

    def pause(self):
        self.paused = True
        self._pending_auto_plot_chunks = []

    def resume(self):
        self.paused = False


@pytest.mark.asyncio
async def test_pausing_cancels_background_work():
    from app.services.game_session import GameSession

    session = object.__new__(GameSession)
    session.paused = False
    session._pending_auto_plot_chunks = ["[AUTO_PLOT]{}"]
    session._pending_tasks = set()
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(30)

    task = asyncio.ensure_future(forever())
    session._pending_tasks.add(task)
    await started.wait()
    session.pause()
    await asyncio.sleep(0)
    assert session.paused is True
    assert task.cancelled() or task.done()
    assert session._pending_auto_plot_chunks == []


@pytest.mark.asyncio
async def test_a_paused_session_refuses_to_take_a_turn():
    from app.services.game_session import GameSession

    session = object.__new__(GameSession)
    session.paused = True
    with pytest.raises(RuntimeError, match="paused"):
        async for _chunk in session.process_action("look around"):
            pass


def test_resume_reopens_the_session():
    from app.services.game_session import GameSession

    session = object.__new__(GameSession)
    session.paused = True
    session.resume()
    assert session.paused is False


@pytest.mark.asyncio
async def test_a_pause_holds_while_the_event_loop_keeps_running():
    """Ten seconds of loop time must not move a paused session."""
    from app.services.game_session import GameSession

    session = object.__new__(GameSession)
    session.paused = False
    session._pending_auto_plot_chunks = ["[AUTO_PLOT]{}"]
    session._pending_tasks = set()
    ticks = []

    async def tick_forever():
        while True:
            ticks.append(1)
            await asyncio.sleep(0.001)

    task = asyncio.ensure_future(tick_forever())
    session._pending_tasks.add(task)
    await asyncio.sleep(0.01)
    assert ticks, "the tick should be running before the player leaves"
    session.pause()
    await asyncio.sleep(0.01)
    seen = len(ticks)
    await asyncio.sleep(0.05)
    assert len(ticks) == seen, "a paused session must not keep ticking"
    assert session.paused is True


@pytest.mark.asyncio
async def test_resuming_reopens_without_catching_up():
    from app.services.game_session import GameSession

    session = object.__new__(GameSession)
    session.paused = True
    session.resume()
    assert session.paused is False
    assert getattr(session, "_pending_auto_plot_chunks", []) == []


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenario.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def test_clock_reports_an_unloaded_campaign_as_idle(client):
    payload = client.get("/api/game/nople/clock").json()
    assert payload["state"] == "idle"
    assert payload["session_cached"] is False


def test_pause_and_resume_round_trip_through_the_api(client):
    from app.api import routes_game

    routes_game._sessions["c-pause"] = FakeSession()
    try:
        assert client.post("/api/game/c-pause/pause").json()["paused"] is True
        assert client.get("/api/game/c-pause/clock").json()["state"] == "paused"
        assert client.post("/api/game/c-pause/resume").json()["paused"] is False
        assert client.get("/api/game/c-pause/clock").json()["state"] == "running"
    finally:
        routes_game._sessions.pop("c-pause", None)


def test_pausing_an_unloaded_campaign_is_harmless(client):
    payload = client.post("/api/game/nobody/pause").json()
    assert payload["paused"] is True
    assert payload["session_cached"] is False