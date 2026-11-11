import pytest

from app.db.scenario_store import ScenarioStore


@pytest.fixture
def store(tmp_path):
    with ScenarioStore(str(tmp_path / "characters.db")) as s:
        yield s


@pytest.fixture
def scenario(store):
    return store.create_scenario(title="雾港", language="zh")


def test_character_with_looks_roundtrip(store, scenario):
    hero = store.add_character(
        scenario.id, name="林岚", role="游侠", description="沉默的斥候", is_player=True
    )
    store.add_look(hero.id, "旅装", "平静", "/media/characters/a.png")
    store.add_look(hero.id, "旅装", "警觉", "/media/characters/b.png")
    store.add_look(hero.id, "礼服", "微笑", "/media/characters/c.png")

    loaded = store.get_character(hero.id)
    assert loaded is not None
    assert loaded.name == "林岚"
    assert loaded.is_player is True
    assert [(l.outfit, l.expression) for l in loaded.looks] == [
        ("旅装", "平静"),
        ("旅装", "警觉"),
        ("礼服", "微笑"),
    ]


def test_look_for_exact_then_outfit_fallback(store, scenario):
    hero = store.add_character(scenario.id, name="林岚")
    store.add_look(hero.id, "旅装", "平静", "/media/x.png")

    loaded = store.get_character(hero.id)
    assert loaded.look_for("旅装", "平静").image_url == "/media/x.png"
    # Same outfit, unknown expression -> still shows the outfit art.
    assert loaded.look_for("旅装", "狂怒").image_url == "/media/x.png"
    assert loaded.look_for("礼服", "平静") is None


def test_list_characters_puts_player_first(store, scenario):
    store.add_character(scenario.id, name="旅店老板")
    store.add_character(scenario.id, name="林岚", is_player=True)

    names = [c.name for c in store.list_characters(scenario.id)]
    assert names == ["林岚", "旅店老板"]


def test_update_and_delete_character_removes_looks(store, scenario):
    hero = store.add_character(scenario.id, name="林岚")
    look = store.add_look(hero.id, "旅装", "平静")

    assert store.update_character(hero.id, role="斥候", name="林岚·改")
    assert store.get_character(hero.id).name == "林岚·改"

    assert store.delete_character(hero.id) is True
    assert store.get_character(hero.id) is None
    assert store.get_look(look.id) is None


def test_update_look_image(store, scenario):
    hero = store.add_character(scenario.id, name="林岚")
    look = store.add_look(hero.id, "旅装", "平静")

    assert store.update_look(look.id, image_url="/media/new.png", expression="微笑")
    updated = store.get_look(look.id)
    assert updated.image_url == "/media/new.png"
    assert updated.expression == "微笑"


def test_campaign_character_picks_are_persisted(store, scenario):
    campaign = store.create_campaign(scenario.id, "旅人")
    assert campaign.character_picks == {}

    hero = store.add_character(scenario.id, name="林岚", is_player=True)
    look = store.add_look(hero.id, "旅装", "警觉", "/media/a.png")
    picks = {
        "character_id": hero.id,
        "look_id": look.id,
        "outfit": "旅装",
        "expression": "警觉",
        "name": "林岚",
    }
    assert store.update_campaign_character(campaign.id, picks)

    reloaded = store.get_campaign(campaign.id)
    assert reloaded.character_picks == picks


def test_delete_scenario_removes_characters_and_looks(store, scenario):
    hero = store.add_character(scenario.id, name="林岚")
    look = store.add_look(hero.id, "旅装", "平静")

    assert store.delete_scenario(scenario.id) is True
    assert store.get_character(hero.id) is None
    assert store.get_look(look.id) is None

def test_library_characters_belong_to_every_scenario(store, scenario):
    library = store.add_character("", name="雾行者", role="游侠")
    own = store.add_character(scenario.id, name="当地人")
    other = store.create_scenario(title="另一世界", language="zh")
    store.add_character(other.id, name="彼方人")

    assert library.scenario_id == ""
    assert [c.id for c in store.list_library_characters()] == [library.id]
    # A library character is offered everywhere; the other world member is not.
    assert {c.id for c in store.list_characters(scenario.id)} == {library.id, own.id}
    assert [c.id for c in store.list_characters(other.id)][0] == library.id


def test_deleting_a_scenario_keeps_the_library(store, scenario):
    library = store.add_character("", name="雾行者")
    store.add_character(scenario.id, name="当地人")

    assert store.delete_scenario(scenario.id) is True
    assert [c.id for c in store.list_library_characters()] == [library.id]


def test_migration_keeps_existing_characters(tmp_path):
    """A database written before characters became library-wide still opens."""
    import sqlite3

    path = tmp_path / "legacy.db"
    with ScenarioStore(str(path)) as s:
        hero = s.add_character("sc1", name="旧人", role="r", is_player=True)
        s.add_look(hero.id, "旅装", "平静", "/media/legacy.png")

    # Pretend the file predates migration 13 and re-open it.
    conn = sqlite3.connect(str(path))
    conn.execute("DELETE FROM schema_version WHERE version = 13")
    conn.commit()
    conn.close()

    with ScenarioStore(str(path)) as s:
        assert s._get_schema_version() >= 13
        loaded = s.get_character(hero.id)
        assert loaded is not None and loaded.scenario_id == "sc1"
        assert [l.image_url for l in loaded.looks] == ["/media/legacy.png"]
        assert [c.id for c in s.list_characters("sc1")] == [hero.id]
