"""World presets: the authored starter worlds are inspectable before binding."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game.db"))
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenario.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


def test_world_presets_are_listed_with_typed_contract(client):
    response = client.get("/api/v1/world-presets")
    assert response.status_code == 200
    payload = response.json()
    keys = [entry["world_type"] for entry in payload]
    assert keys == ["fantasy", "fog_town", "orbital_station", "wild_camp"]
    station = next(entry for entry in payload if entry["world_type"] == "orbital_station")
    assert station["name"] == "Orbital Station Incident"
    assert station["locations"] == 4
    assert station["doors"] == 2
    assert station["quests"] == 1


def test_world_presets_are_read_only(client):
    from app.db.game_state_store import GameStateStore
    from app.api import routes_engine

    store = routes_engine._get_store()
    try:
        before = len(store.list_worlds())
    finally:
        store.close()
    assert client.get("/api/v1/world-presets").status_code == 200
    store = routes_engine._get_store()
    try:
        assert len(store.list_worlds()) == before
    finally:
        store.close()
