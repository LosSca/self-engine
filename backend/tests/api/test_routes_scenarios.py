import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Patch database paths to use temp files
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app
    return TestClient(app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_scenario(client):
    r = client.post("/api/scenarios/", json={
        "title": "Dark Realm",
        "description": "A world of shadows",
        "tone_instructions": "Dark and hopeless",
        "opening_narrative": "You wake in darkness...",
        "language": "en",
    })
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Dark Realm"
    assert "id" in data


def test_list_scenarios(client):
    # Create one first
    client.post("/api/scenarios/", json={
        "title": "Test World", "description": "", "tone_instructions": "",
        "opening_narrative": "", "language": "en"
    })
    r = client.get("/api/scenarios/")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert len(r.json()) >= 1


def test_get_scenario_not_found(client):
    r = client.get("/api/scenarios/nonexistent-id")
    assert r.status_code == 404


def test_add_story_card(client):
    # Create scenario first
    scenario = client.post("/api/scenarios/", json={
        "title": "Test", "description": "", "tone_instructions": "",
        "opening_narrative": "", "language": "en"
    }).json()
    r = client.post(f"/api/scenarios/{scenario['id']}/story-cards", json={
        "card_type": "NPC",
        "name": "Mordain",
        "content": {"power_level": 9},
    })
    assert r.status_code == 201
    assert r.json()["name"] == "Mordain"


def test_export_scenario(client):
    # Create scenario with a story card and campaign
    scenario = client.post("/api/scenarios/", json={
        "title": "Export World",
        "description": "desc",
        "tone_instructions": "gritty",
        "opening_narrative": "begin",
        "language": "en",
        "lore_text": "lots of lore",
    }).json()
    client.post(f"/api/scenarios/{scenario['id']}/story-cards", json={
        "card_type": "NPC", "name": "Aria", "content": {"age": 30},
    })
    # Export
    r = client.get(f"/api/scenarios/{scenario['id']}/export")
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == "1.1"
    assert data["scenario"]["title"] == "Export World"
    assert "id" not in data["scenario"]
    assert "created_at" not in data["scenario"]
    assert len(data["story_cards"]) == 1
    assert data["story_cards"][0]["name"] == "Aria"
    assert data["story_cards"][0]["card_type"] == "NPC"
    assert isinstance(data["campaigns"], list)
    assert "exported_at" in data
    # Newer exports carry authored knowledge and the play mode, even when empty.
    assert data["knowledge"] == []
    assert data["play_config"]["mode"] == "storyline"
    assert [node["id"] for node in data["play_config"]["storyline"]["nodes"]] == [
        "opening",
        "first_turn",
    ]


def test_export_scenario_not_found(client):
    r = client.get("/api/scenarios/nonexistent/export")
    assert r.status_code == 404


def test_import_scenario(client):
    payload = {
        "version": "1.0",
        "exported_at": "2026-01-01T00:00:00",
        "scenario": {
            "title": "Imported World",
            "description": "imported desc",
            "tone_instructions": "epic",
            "opening_narrative": "chapter one",
            "language": "pt-br",
            "lore_text": "ancient lore",
        },
        "story_cards": [
            {"card_type": "LOCATION", "name": "The Citadel", "content": {"size": "huge"}},
        ],
        "campaigns": [
            {"player_name": "Hero"},
        ],
    }
    r = client.post("/api/scenarios/import", json=payload)
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Imported World"
    assert data["language"] == "pt-br"
    assert "id" in data

    # Verify story cards were created
    scenario_id = data["id"]
    cards = client.get(f"/api/scenarios/{scenario_id}/story-cards").json()
    assert len(cards) == 1
    assert cards[0]["name"] == "The Citadel"
    assert cards[0]["card_type"] == "LOCATION"

    campaigns_resp = client.get(f"/api/scenarios/{scenario_id}/campaigns")
    assert campaigns_resp.status_code == 200
    campaign_list = campaigns_resp.json()
    assert len(campaign_list) == 1
    assert campaign_list[0]["player_name"] == "Hero"


def test_import_scenario_missing_scenario_key(client):
    r = client.post("/api/scenarios/import", json={"version": "1.0"})
    assert r.status_code == 422

def test_export_import_roundtrip_carries_knowledge_and_storyline(client):
    scenario = client.post("/api/scenarios/", json={"title": "Lore World"}).json()
    first = client.post(f"/api/v1/scenarios/{scenario['id']}/knowledge", json={
        "layer": "global",
        "title": "世界总览",
        "content": "海那边有一座塔。",
        "tags": ["world"],
        "depth": 1,
        "always_on": True,
    }).json()
    second = client.post(f"/api/v1/scenarios/{scenario['id']}/knowledge", json={
        "layer": "story",
        "title": "仓库线",
        "content": "仓库门后藏着账本。",
        "depth": 4,
        "mode_scope": "storyline",
    }).json()
    storyline = {
        "name": "仓库线",
        "description": "",
        "nodes": [{
            "id": "arrive",
            "title": "抵达村庄",
            "kind": "main",
            "summary": "",
            "knowledge_ids": [second["id"]],
        }],
    }
    r = client.put(f"/api/v1/scenarios/{scenario['id']}/play-config", json={
        "mode": "storyline", "storyline": storyline, "max_depth": 6,
    })
    assert r.status_code == 200, r.text

    exported = client.get(f"/api/scenarios/{scenario['id']}/export").json()
    assert [e["title"] for e in exported["knowledge"]] == ["世界总览", "仓库线"]
    assert exported["play_config"]["mode"] == "storyline"
    assert exported["play_config"]["max_depth"] == 6

    r = client.post("/api/scenarios/import", json=exported)
    assert r.status_code == 201, r.text
    imported_id = r.json()["id"]
    entries = client.get(
        f"/api/v1/scenarios/{imported_id}/knowledge?include_disabled=true"
    ).json()
    assert [e["title"] for e in entries] == ["世界总览", "仓库线"]
    # Export ids are per-install; the import must remap storyline references.
    assert entries[0]["id"] != first["id"]
    config = client.get(f"/api/v1/scenarios/{imported_id}/play-config").json()
    assert config["mode"] == "storyline"
    node = config["storyline"]["nodes"][0]
    assert node["knowledge_ids"] == [entries[1]["id"]]


def test_import_rejects_bad_storyline_and_rolls_back(client):
    payload = {
        "version": "1.1",
        "scenario": {"title": "Broken World"},
        "knowledge": [
            {"id": "k1", "layer": "story", "title": "片段", "content": "x"},
        ],
        "play_config": {
            "mode": "storyline",
            "storyline": {
                "name": "破损线",
                "nodes": [{"id": "b", "title": "孤儿", "kind": "main", "prereq": ["ghost"]}],
            },
        },
    }
    r = client.post("/api/scenarios/import", json=payload)
    assert r.status_code == 400
    # A rejected import must not leave a half-written scenario behind.
    assert client.get("/api/scenarios/").json() == []


def test_draft_returns_template_text_for_every_kind(client, monkeypatch):
    # No model configured: the endpoint must still answer, using templates.
    import app.services.draft_generator as dg

    assert dg.DRAFT_KINDS
    for kind in ("character", "world", "location", "faction", "item", "lore"):
        r = client.post("/api/scenarios/draft", json={"kind": kind, "topic": "北境", "language": "zh"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider"] in {"llm", "template"}
        assert isinstance(body["text"], str) and body["text"].strip()
        assert "北境" in body["text"]


def test_draft_rejects_unknown_kind(client):
    r = client.post("/api/scenarios/draft", json={"kind": "nonsense"})
    assert r.status_code == 400
    assert "nonsense" in r.json()["detail"]
    titles = [s["title"] for s in client.get("/api/scenarios/").json()]
    assert "Broken World" not in titles