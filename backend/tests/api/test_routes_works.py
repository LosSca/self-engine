import pytest

from tests.conftest import DEFAULT_OPENING as OPENING
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


def test_catalogue_lists_curated_works(client):
    res = client.get("/api/v1/works")
    assert res.status_code == 200, res.text
    works = res.json()
    assert len(works) >= 6
    for work in works:
        assert work["source"] == "curated"
        assert work["characters"], work["title"]
        assert work["locations"], work["title"]


def test_get_work_is_curated_only(client):
    res = client.get("/api/v1/works/福尔摩斯")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["title"] == "福尔摩斯探案集"
    names = [row["name"] for row in body["characters"]]
    assert "夏洛克·福尔摩斯" in names


def test_unknown_work_is_404_not_a_model_call(client):
    res = client.get("/api/v1/works/这本书不存在")
    assert res.status_code == 404


def test_resolve_refuses_to_spend_a_model_call_without_consent(client):
    res = client.post("/api/v1/works/resolve", json={"title": "不存在的作品", "allow_model": False})
    assert res.status_code == 404


def test_resolve_hits_the_catalogue_before_the_model(client):
    res = client.post("/api/v1/works/resolve", json={"title": "指环王", "allow_model": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "curated"
    assert body["title"] == "指环王"


def test_enter_work_saves_a_connected_world(client):
    res = client.post(
        "/api/v1/works/enter",
        json={"work_title": "福尔摩斯", "character_name": "约翰·华生"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "saved"
    assert body["world_id"] == "crossover_" + "sherlock_holmes"
    assert body["locations"] >= 3
    assert body["character"]["name"] == "约翰·华生"

    world = client.get(f"/api/v1/worlds/{body['world_id']}")
    assert world.status_code == 200, world.text
    document = world.json()
    assert document["start_location_id"]
    # Every location must be reachable from the start, otherwise the world
    # skeleton would pass validation but trap the player.
    edges = {loc["id"]: [exit_["target_id"] for exit_ in loc["exits"]] for loc in document["locations"]}
    seen = {document["start_location_id"]}
    frontier = [document["start_location_id"]]
    while frontier:
        node = frontier.pop()
        for nxt in edges.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(edges)


def test_enter_work_rejects_an_unknown_character(client):
    res = client.post(
        "/api/v1/works/enter",
        json={"work_title": "三体", "character_name": "甘道夫"},
    )
    assert res.status_code == 400
    assert "可选" in res.json()["detail"]


def test_enter_work_records_the_pick_on_the_campaign(client):
    scenario = client.post("/api/scenarios/", json={"title": "穿越", "language": "zh", "opening_narrative": OPENING})
    assert scenario.status_code == 201, scenario.text
    scenario_id = scenario.json()["id"]
    campaign = client.post(
        f"/api/scenarios/{scenario_id}/campaigns", json={"player_name": "旅人"}
    )
    assert campaign.status_code == 201, campaign.text
    campaign_id = campaign.json()["id"]

    res = client.post(
        "/api/v1/works/enter",
        json={
            "work_title": "哈利波特",
            "character_name": "赫敏·格兰杰",
            "scenario_id": scenario_id,
            "campaign_id": campaign_id,
        },
    )
    assert res.status_code == 200, res.text
    pick = res.json()["character_pick"]
    assert pick["name"] == "赫敏·格兰杰"
    assert pick["source_work"] == "哈利·波特"

    stored = client.get(f"/api/campaigns/{campaign_id}/character")
    assert stored.status_code == 200, stored.text
    assert stored.json()["picks"]["name"] == "赫敏·格兰杰"
    assert stored.json()["picks"]["source_work"] == "哈利·波特"