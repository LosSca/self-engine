"""Looking at a thing is investigating the thing, not a bare glance.

"观察X"/"打量X" used to parse as a targetless ``look``, so the subject of the
sentence was thrown away and the turn changed nothing. The rule lane has an
``investigate` action that takes a target; that is what a sentence like
"观察工程师艾拉" means.
"""

from app.models.game import EntityDef, WorldDefinition
from app.services.text_action_service import TextActionParser
from tests.support import fresh_state, village_world


def _world() -> WorldDefinition:
    world = village_world()
    world.entities = [
        EntityDef(id="engineer", name="工程师艾拉", location_id="village_square"),
    ]
    return world


def _parse(text: str):
    world = _world()
    return TextActionParser().parse(text, world, fresh_state(world))


def test_observing_a_person_investigates_them_by_id():
    parsed = _parse("观察工程师艾拉")
    assert parsed.action == "investigate"
    assert parsed.target_id == "engineer"


def test_studying_a_person_investigates_them():
    parsed = _parse("打量工程师艾拉，留意对方的反应")
    assert parsed.action == "investigate"
    assert parsed.target_id == "engineer"


def test_examining_a_room_investigates_the_room():
    """The room is the most common subject of "查看X" in play."""
    world = _world()
    room_name = next(loc.name for loc in world.locations if loc.id == "warehouse")
    parsed = TextActionParser().parse(f"查看{room_name}", world, fresh_state(world))
    assert parsed.action == "investigate"
    assert parsed.target_id == "warehouse"


def test_looking_around_stays_a_bare_look():
    for text in ("观察四周", "环顾四周", "看"):
        parsed = _parse(text)
        assert parsed.action == "look", text
        assert parsed.target_id is None, text
