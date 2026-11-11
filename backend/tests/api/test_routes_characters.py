import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
    from app.main import app

    return TestClient(app)


@pytest.fixture
def scenario_id(client):
    res = client.post(
        "/api/scenarios/",
        json={"title": "雾港", "language": "zh"},
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_character_crud_and_looks(client, scenario_id):
    res = client.post(
        f"/api/scenarios/{scenario_id}/characters",
        json={"name": "林岚", "role": "游侠", "is_player": True},
    )
    assert res.status_code == 201, res.text
    character = res.json()
    assert character["name"] == "林岚"
    assert character["looks"] == []

    res = client.post(
        f"/api/characters/{character['id']}/looks",
        json={"outfit": "旅装", "expression": "平静", "image_url": "/media/x.png"},
    )
    assert res.status_code == 201, res.text
    look = res.json()

    res = client.get(f"/api/scenarios/{scenario_id}/characters")
    assert res.status_code == 200
    listed = res.json()
    assert len(listed) == 1
    assert listed[0]["looks"][0]["image_url"] == "/media/x.png"

    res = client.patch(f"/api/characters/{character['id']}", json={"role": "斥候"})
    assert res.status_code == 200
    assert res.json()["role"] == "斥候"

    res = client.patch(
        f"/api/characters/looks/{look['id']}", json={"expression": "警觉"}
    )
    assert res.status_code == 200
    assert res.json()["expression"] == "警觉"

    res = client.delete(f"/api/characters/looks/{look['id']}")
    assert res.status_code == 200
    assert client.get(f"/api/characters/{character['id']}").json()["looks"] == []

    assert client.delete(f"/api/characters/{character['id']}").status_code == 200
    assert client.get(f"/api/characters/{character['id']}").status_code == 404


def test_duplicate_look_is_rejected(client, scenario_id):
    character = client.post(
        f"/api/scenarios/{scenario_id}/characters", json={"name": "林岚"}
    ).json()
    body = {"outfit": "旅装", "expression": "平静"}
    assert client.post(f"/api/characters/{character['id']}/looks", json=body).status_code == 201
    res = client.post(f"/api/characters/{character['id']}/looks", json=body)
    assert res.status_code == 409
    assert "already exists" in res.json()["detail"]


def test_campaign_character_selection(client, scenario_id):
    campaign = client.post(
        f"/api/scenarios/{scenario_id}/campaigns", json={"player_name": "旅人"}
    ).json()
    character = client.post(
        f"/api/scenarios/{scenario_id}/characters",
        json={"name": "林岚", "is_player": True},
    ).json()
    look = client.post(
        f"/api/characters/{character['id']}/looks",
        json={"outfit": "旅装", "expression": "警觉", "image_url": "/media/a.png"},
    ).json()

    res = client.get(f"/api/campaigns/{campaign['id']}/character")
    assert res.status_code == 200
    assert res.json()["character"] is None

    res = client.put(
        f"/api/campaigns/{campaign['id']}/character",
        json={"character_id": character["id"], "look_id": look["id"]},
    )
    assert res.status_code == 200, res.text
    picks = res.json()["picks"]
    assert picks["outfit"] == "旅装" and picks["expression"] == "警觉"

    res = client.get(f"/api/campaigns/{campaign['id']}/character")
    body = res.json()
    assert body["character"]["name"] == "林岚"
    assert body["look"]["image_url"] == "/media/a.png"


def test_campaign_accepts_a_character_from_another_scenario(client, scenario_id):
    """A character is library-wide: any world may bring it in.

    This used to be a 409 ("Character belongs to another scenario"); the lock
    was removed when characters moved out of the scenario.
    """
    other = client.post("/api/scenarios/", json={"title": "别的世界"}).json()
    foreign = client.post(
        f"/api/scenarios/{other['id']}/characters", json={"name": "外来者"}
    ).json()
    campaign = client.post(
        f"/api/scenarios/{scenario_id}/campaigns", json={"player_name": "旅人"}
    ).json()

    res = client.put(
        f"/api/campaigns/{campaign['id']}/character",
        json={"character_id": foreign["id"]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["picks"]["character_id"] == foreign["id"]
    assert res.json()["picks"]["name"] == "外来者"


def test_library_characters_are_created_outside_any_scenario(client):
    created = client.post(
        "/api/characters",
        json={"name": "雾行者", "role": "游侠", "description": "来自北境", "is_player": True},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["scenario_id"] == ""
    assert body["is_player"] is True

    listed = client.get("/api/characters").json()
    assert [c["id"] for c in listed] == [body["id"]]

    # ...and it is offered by every scenario without being copied into it.
    scenario = client.post("/api/scenarios/", json={"title": "某个世界"}).json()
    offered = client.get(f"/api/scenarios/{scenario['id']}/characters").json()
    assert body["id"] in [c["id"] for c in offered]


def test_scenario_character_can_be_imported_into_the_library(client, scenario_id):
    owned = client.post(
        f"/api/scenarios/{scenario_id}/characters",
        json={"name": "本世界的人", "role": "村民"},
    ).json()
    client.post(
        f"/api/characters/{owned['id']}/looks",
        json={"outfit": "常服", "expression": "平静", "image_url": "/media/x.png"},
    )

    res = client.post(f"/api/characters/{owned['id']}/library/import")
    assert res.status_code == 201, res.text
    clone = res.json()
    assert clone["scenario_id"] == ""
    assert clone["name"] == "本世界的人"
    assert [look["outfit"] for look in clone["looks"]] == ["常服"]
    assert clone["id"] != owned["id"]

    # Importing an already-library character is a no-op, not a duplicate.
    again = client.post(f"/api/characters/{clone['id']}/library/import")
    assert again.status_code == 201
    assert again.json()["id"] == clone["id"]
    assert len(client.get("/api/characters").json()) == 1


def test_upload_writes_into_media_dir(client, tmp_path, monkeypatch):
    from app.api import routes_characters
    from app.config import settings

    media = tmp_path / "media"
    monkeypatch.setattr(settings, "media_dir", str(media))

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    res = client.post(
        "/api/characters/upload",
        files={"file": ("portrait.png", io.BytesIO(png), "image/png")},
    )
    assert res.status_code == 200, res.text
    url = res.json()["url"]
    assert url.startswith("/media/characters/")
    assert (media / "characters" / url.rsplit("/", 1)[1]).read_bytes() == png


def test_upload_rejects_non_image(client):
    res = client.post(
        "/api/characters/upload",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert res.status_code == 400
    assert "Unsupported image type" in res.json()["detail"]



def test_import_scenario_creates_world_type_and_character_pack(client):
    payload = {
        "version": "1.0",
        "scenario": {
            "title": "北境石门",
            "language": "zh",
            "world_type": "fantasy",
            "setup_questions": [
                {
                    "id": "q_origin",
                    "var_name": "origin",
                    "prompt": "你从哪来？",
                    "type": "choice",
                    "options": [{"label": "雾港"}, {"label": "内陆"}],
                    "allow_custom": True,
                    "required": True,
                }
            ],
        },
        "story_cards": [{"card_type": "NPC", "name": "赫伯", "content": {"personality": "谨慎"}}],
        "campaigns": [],
        "characters": [
            {
                "name": "旅人",
                "role": "剑客",
                "is_player": True,
                "looks": [
                    {"outfit": "常服", "expression": "平静", "image_url": "/media/a.png"},
                    {"outfit": "常服", "expression": "微笑", "image_url": "/media/b.png"},
                ],
            }
        ],
    }
    res = client.post("/api/scenarios/import", json=payload)
    assert res.status_code == 201, res.text
    scenario = res.json()
    assert scenario["world_type"] == "fantasy"
    scenario_id = scenario["id"]

    characters = client.get(f"/api/scenarios/{scenario_id}/characters").json()
    assert [c["name"] for c in characters] == ["旅人"]
    assert characters[0]["is_player"] is True
    assert [look["expression"] for look in characters[0]["looks"]] == ["平静", "微笑"]

    export = client.get(f"/api/scenarios/{scenario_id}/export").json()
    assert export["scenario"]["world_type"] == "fantasy"
    assert export["characters"][0]["looks"][1]["image_url"] == "/media/b.png"


def test_campaign_character_falls_back_to_same_outfit(client, scenario_id):
    campaign = client.post(
        f"/api/scenarios/{scenario_id}/campaigns", json={"player_name": "旅人"}
    ).json()
    character = client.post(
        f"/api/scenarios/{scenario_id}/characters",
        json={"name": "林岚", "is_player": True},
    ).json()
    look = client.post(
        f"/api/characters/{character['id']}/looks",
        json={"outfit": "旅装", "expression": "平静", "image_url": "/media/a.png"},
    ).json()

    # 只记录服装与表情、没有 look_id 时，仍应解析出同服装的立绘。
    res = client.put(
        f"/api/campaigns/{campaign['id']}/character",
        json={"character_id": character["id"], "outfit": "旅装", "expression": "警觉"},
    )
    assert res.status_code == 200, res.text

    body = client.get(f"/api/campaigns/{campaign['id']}/character").json()
    assert body["look"]["id"] == look["id"]
    assert body["look"]["outfit"] == "旅装"


def test_scenario_view_carries_world_type_and_characters(client):
    payload = {
        "version": "1.0",
        "scenario": {"title": "雾港旧事", "language": "zh", "world_type": "mystery"},
        "story_cards": [],
        "campaigns": [],
        "characters": [
            {
                "name": "沈默",
                "role": "记者",
                "is_player": True,
                "looks": [{"outfit": "常服", "expression": "平静", "image_url": "/media/s.png"}],
            }
        ],
    }
    scenario = client.post("/api/scenarios/import", json=payload).json()
    campaign = client.post(
        f"/api/scenarios/{scenario['id']}/campaigns", json={"player_name": "旅人"}
    ).json()

    res = client.get(f"/api/game/{campaign['id']}/scenario-view")
    assert res.status_code == 200, res.text
    view = res.json()
    assert view["world_type"] == "mystery"
    assert [c["name"] for c in view["characters"]] == ["沈默"]
    assert view["characters"][0]["looks"][0]["image_url"] == "/media/s.png"
