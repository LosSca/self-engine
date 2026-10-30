"""Deterministic utterance parsing: verb + target resolution."""
import pytest

from app.db.game_state_store import GameStateStore
from app.engines.event_engine import apply_effects
from app.models.game import ActionRequest, CustomActionDef, Effect, EntityDef, WorldDefinition
from app.services.text_action_service import TextActionParser, TextActionService
from app.services.world_service import bind_world, save_world
from tests.support import fresh_state, village_world, village_world_data


def _chinese_world() -> WorldDefinition:
    """The demo world with Chinese display names, ids kept in ASCII."""
    data = village_world_data()
    for item in data["items"]:
        if item["id"] == "iron_key":
            item["name"] = "铁钥匙"
    for door in data["doors"]:
        if door["id"] == "warehouse_door":
            door["name"] = "仓库门"
    for location in data["locations"]:
        if location["id"] in ("village_square", "forest_edge", "warehouse"):
            location["name"] = {"village_square": "村庄广场", "forest_edge": "森林边缘", "warehouse": "仓库"}[location["id"]]
    return WorldDefinition.model_validate(data)


def test_ascii_id_resolves_english_spelling():
    """An author id of iron_key accepts "iron key" typed by the player."""
    world = _chinese_world()
    state = fresh_state(world)
    parsed = TextActionParser().parse("pick up the iron key", world, state)
    assert parsed is not None
    assert parsed.action == "take"
    assert parsed.target_id == "iron_key"


def test_underscore_id_resolves_when_player_types_underscore():
    world = _chinese_world()
    state = fresh_state(world)
    parsed = TextActionParser().parse("open warehouse_door", world, state)
    assert parsed is not None
    assert parsed.action == "open"
    assert parsed.target_id == "warehouse_door"


def test_chinese_display_name_still_matches():
    world = _chinese_world()
    state = fresh_state(world)
    parsed = TextActionParser().parse("打开仓库门", world, state)
    assert parsed is not None
    assert parsed.action == "open"
    assert parsed.target_id == "warehouse_door"


def test_unknown_target_is_reported_not_guessed():
    world = _chinese_world()
    state = fresh_state(world)
    parsed = TextActionParser().parse("take the moon", world, state)
    assert parsed is not None
    assert parsed.action == "take"
    assert parsed.target_id is None
    assert parsed.confidence == "missing_target"


def _entity_world():
    world = village_world()
    world.entities = [
        EntityDef(id="guard", name="Sentry", location_id="village_square", hostile=True,
                  attributes={"max_hp": 30, "flee_chance": 1.0}),
        EntityDef(id="wolf", name="Wolf", location_id="forest_edge", kind="creature"),
        EntityDef(id="chest", name="Chest", location_id="village_square", kind="object"),
    ]
    return world


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGINE_INTENT_ADVISOR", raising=False)
    store = GameStateStore(str(tmp_path / "text_actions.db"))
    world = _entity_world()
    save_world(store, world)
    bind_world(store, "camp-1", world.world_id)
    yield TextActionService(store)
    store.close()


@pytest.mark.parametrize("text, action, target", [
    ("[DO] pick up the iron key", "take", "iron_key"),
    ("[do] open warehouse_door", "open", "warehouse_door"),
    ("[DO] attack the guard", "attack", "guard"),
    ("[SAY] talk to the guard", "talk", "guard"),
    ("[SAY] Sentry", "talk", "guard"),
    ("iron key", "take", "iron_key"),
    ("north", "move", "forest_edge"),
    ("Sentry", "talk", "guard"),
])
def test_envelopes_and_bare_exact_aliases_preserve_intent(text, action, target):
    world = _entity_world()
    parsed = TextActionParser().parse(text, world, fresh_state(world))
    assert parsed.action == action
    assert parsed.target_id == target


@pytest.mark.parametrize("text", [
    "sneak past guard", "investigate Chest", "iron", "please steal iron_key",
    "lookout guard", "openly iron_key", "takeover iron_key", "guardrail",
    "[SAY] take iron_key", "[SAY] attack guard", "[META] take iron_key",
    "[CONTINUE]", "[THINK] take iron_key", "[DO] sneak past guard",
])
async def test_unsupported_sentences_and_envelopes_do_not_mutate(service, text):
    store = service.game_store
    before = store.get_state("camp-1").model_dump_json()
    result = await service.execute("camp-1", text, narrate=False)
    assert result["status"] == "rejected"
    assert result["result"]["code"] == "unparsed_action"
    assert store.get_state("camp-1").model_dump_json() == before
    assert store.list_events("camp-1") == []


@pytest.mark.parametrize("text, action", [
    ("open iron_key", "open"), ("take warehouse_door", "take"),
    ("talk iron_key", "talk"), ("use guard", "use"),
])
async def test_an_explicit_wrong_verb_never_changes_action(service, text, action):
    before = service.game_store.get_state("camp-1").model_dump_json()
    result = await service.execute("camp-1", text, narrate=False)
    assert result["status"] == "rejected"
    assert result["parsed"]["action"] == action
    assert service.game_store.get_state("camp-1").model_dump_json() == before
    assert service.game_store.list_events("camp-1") == []


@pytest.mark.parametrize("text", ["look Chest", "look iron_key", "look nonexistent"])
async def test_targeted_look_is_explicitly_unsupported(service, text):
    before = service.game_store.get_state("camp-1").model_dump_json()
    result = await service.execute("camp-1", text, narrate=False)
    assert result["status"] == "rejected"
    assert result["result"]["code"] == "unsupported_target"
    assert result["parsed"]["target_id"]
    assert service.game_store.get_state("camp-1").model_dump_json() == before


async def test_ui_action_replay_uses_current_state_without_reparsing(service, monkeypatch):
    first = await service.execute("camp-1", "[DO] take iron_key", "ui-take", narrate=False)
    assert first["status"] == "applied"
    await service.execute("camp-1", "[DO] go to forest_edge", "ui-move", narrate=False)
    current = service.game_store.get_state("camp-1").model_dump(mode="json")
    events = service.game_store.list_events("camp-1")

    def unexpected_parse(*args):
        pytest.fail("a replay must not reparse against a changed location")

    monkeypatch.setattr(service.parser, "parse", unexpected_parse)
    replay = await service.execute("camp-1", "[DO] take iron_key", "ui-take", narrate=True)
    assert replay["status"] == replay["result"]["status"] == "duplicate"
    assert replay["result"]["state"] == current
    assert replay["result"]["state_version"] == 2
    assert service.game_store.get_state("camp-1").inventory == {"iron_key": 1}
    assert service.game_store.list_events("camp-1") == events


@pytest.mark.parametrize("original", ["[DO] take iron_key", "sneak past guard", "talk nonexistent"])
async def test_reused_text_id_conflicts_across_campaigns_and_changed_text(service, original):
    store = service.game_store
    await service.execute("camp-1", original, "shared", narrate=False)
    bind_world(store, "camp-2", "demo_village")
    original_row = store.get_request("shared")
    before = {camp: store.get_state(camp).model_dump_json() for camp in ("camp-1", "camp-2")}
    for campaign, text in (("camp-2", original), ("camp-1", "look"), ("unbound", original)):
        out = await service.execute(campaign, text, "shared", narrate=False)
        assert out["status"] == out["result"]["status"] == "conflict"
        assert out["result"]["code"] == "request_id_conflict"
        assert out["result"]["campaign_id"] == campaign
        assert out["result"]["effects_applied"] == []
        assert out["result"]["events_fired"] == []
        assert out["result"]["narrative"]["text"] == ""
        if campaign == "unbound":
            assert out["result"]["state"] is None
            assert out["result"]["state_version"] == 0
        else:
            assert out["result"]["state"]["campaign_id"] == campaign
    assert store.get_request("shared") == original_row
    assert {camp: store.get_state(camp).model_dump_json() for camp in before} == before


@pytest.mark.parametrize("params, actor", [({}, "player"), ({"text": "look"}, "npc")])
async def test_structured_ids_need_matching_text_and_actor(service, params, actor):
    request = ActionRequest(campaign_id="camp-1", request_id="structured", action="look",
                            params=params, actor_id=actor, narrate=False)
    await service.action_service.execute(request)
    before = service.game_store.get_state("camp-1").model_dump_json()
    result = await service.execute("camp-1", "look", "structured", narrate=False)
    assert result["status"] == "conflict"
    assert result["result"]["code"] == "request_id_conflict"
    assert service.game_store.get_state("camp-1").model_dump_json() == before


@pytest.mark.parametrize("text", ["sneak past guard", "talk nonexistent", "attack nonexistent"])
async def test_rejected_text_replay_keeps_rejection_after_state_changes(service, text, monkeypatch):
    first = await service.execute("camp-1", text, "rejected", narrate=False)
    assert first["status"] == "rejected"
    await service.execute("camp-1", "go to forest_edge", "move", narrate=False)
    current = service.game_store.get_state("camp-1").model_dump(mode="json")
    monkeypatch.setattr(service.parser, "parse", lambda *args: pytest.fail("reparsed a rejection"))
    replay = await service.execute("camp-1", text, "rejected", narrate=False)
    assert replay["status"] == replay["result"]["status"] == "duplicate"
    assert not replay["result"]["ok"]
    assert replay["result"]["code"] == first["result"]["code"]
    assert replay["result"]["state"] == current
    assert replay["result"]["state_version"] == current["version"]


async def test_unparsed_record_race_does_not_expose_the_winning_campaign(service, monkeypatch):
    store = service.game_store
    bind_world(store, "camp-2", "demo_village")
    await service.execute("camp-2", "take iron_key", "race", narrate=False)
    get_request = store.get_request
    calls = 0

    def raced_lookup(request_id):
        nonlocal calls
        calls += 1
        return None if calls == 1 else get_request(request_id)

    monkeypatch.setattr(store, "get_request", raced_lookup)
    result = await service.execute("camp-1", "sneak past guard", "race", narrate=False)
    assert result["status"] == "conflict"
    assert result["result"]["campaign_id"] == "camp-1"
    assert result["result"]["state"]["inventory"] == {}
    assert result["result"]["effects_applied"] == []


@pytest.mark.parametrize("text", ["attack missing", "attack imaginary guard", "defend missing"])
async def test_explicit_missing_combat_target_never_uses_the_active_enemy(service, text):
    store = service.game_store
    state = store.get_state("camp-1")
    state.combat = {"active": True, "target_id": "guard"}
    state.flags.update(combat_active=True, combat_target="guard")
    store.bind_campaign(state)
    before = state.model_dump_json()
    out = await service.execute("camp-1", text, narrate=False)
    assert out["status"] == "rejected"
    assert out["result"]["code"] == "missing_target"
    assert store.get_state("camp-1").model_dump_json() == before
    assert store.list_events("camp-1") == []


async def test_ambiguous_alias_does_not_select_a_target(service):
    store = service.game_store
    world = store.get_world("demo_village")
    world.entities.append(EntityDef(id="other_guard", name="Sentry", location_id="village_square", hostile=True))
    save_world(store, world)
    before = store.get_state("camp-1").model_dump_json()
    for text in ("Sentry", "attack Sentry", "talk Sentry", "attack"):
        out = await service.execute("camp-1", text, narrate=False)
        assert out["status"] == "rejected", text
    assert store.get_state("camp-1").model_dump_json() == before


async def test_text_talk_and_attack_follow_moved_entities(service):
    store = service.game_store
    world = store.get_world("demo_village")
    state = store.get_state("camp-1")
    apply_effects(state, world, [Effect(kind="move_entity", target="guard", value="forest_edge")])
    state.combat = {"target_id": "guard"}
    store.bind_campaign(state)
    before = state.model_dump_json()
    for text in ("talk guard", "attack guard", "attack"):
        result = await service.execute("camp-1", text, narrate=False)
        assert result["status"] == "rejected"
    assert store.get_state("camp-1").model_dump_json() == before
    moved = await service.execute("camp-1", "go to forest_edge", narrate=False)
    assert moved["status"] == "applied"
    for text in ("talk guard", "attack guard"):
        result = await service.execute("camp-1", text, narrate=False)
        assert result["status"] == "applied", result
        assert result["parsed"]["target_id"] == "guard"


async def test_dead_entities_are_not_text_targets(service):
    store = service.game_store
    state = store.get_state("camp-1")
    world = store.get_world("demo_village")
    apply_effects(state, world, [Effect(kind="combat_hp", target="guard", value=0)])
    state.combat = {"target_id": "guard"}
    store.bind_campaign(state)
    before = state.model_dump_json()
    for text in ("Sentry", "talk guard", "attack guard", "attack"):
        out = await service.execute("camp-1", text, narrate=False)
        assert out["status"] == "rejected", text
    assert store.get_state("camp-1").model_dump_json() == before


async def test_attack_object_is_not_supported(service):
    before = service.game_store.get_state("camp-1").model_dump_json()
    result = await service.execute("camp-1", "attack Chest", narrate=False)
    assert result["status"] == "rejected"
    assert result["result"]["code"] == "unsupported_target_kind"
    assert service.game_store.get_state("camp-1").model_dump_json() == before


async def test_move_is_blocked_until_flee_succeeds_for_both_entry_points(service):
    store = service.game_store
    defended = await service.execute("camp-1", "defend guard", narrate=False)
    assert defended["status"] == "applied"
    before = store.get_state("camp-1").model_dump_json()
    text_move = await service.execute("camp-1", "go to forest_edge", narrate=False)
    structured_move = await service.action_service.execute(ActionRequest(
        campaign_id="camp-1", request_id="structured-move", action="move",
        target_id="forest_edge", narrate=False,
    ))
    assert text_move["result"]["code"] == structured_move.code == "combat_active"
    assert text_move["status"] == structured_move.status == "rejected"
    assert store.get_state("camp-1").model_dump_json() == before
    escaped = await service.execute("camp-1", "flee", narrate=False)
    assert escaped["result"]["code"] == "escaped"
    moved = await service.execute("camp-1", "go to forest_edge", narrate=False)
    assert moved["status"] == "applied"


async def test_custom_actions_keep_object_targets_and_ui_envelopes(service):
    store = service.game_store
    world = store.get_world("demo_village")
    world.custom_actions = [CustomActionDef(
        id="bless", verbs=["bless"], target="required",
        effects=[Effect(kind="set_flag", target="blessed", value=True)],
    )]
    save_world(store, world)
    rejected = await service.execute("camp-1", "blessing Chest", narrate=False)
    assert rejected["status"] == "rejected"
    result = await service.execute("camp-1", "[DO] bless Chest", narrate=False)
    assert result["status"] == "applied"
    assert result["parsed"]["params"] == {"action_id": "bless"}
    assert result["parsed"]["target_id"] == "chest"
    assert store.get_state("camp-1").flags["blessed"] is True
