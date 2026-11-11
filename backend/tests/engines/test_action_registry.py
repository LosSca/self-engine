"""The action catalogue is the single source of truth for gameplay verbs."""
import pytest

from app.engines.action_registry import (
    ActionSpec,
    action_names,
    all_actions,
    get_action,
    register_action,
    verb_table,
)
from app.models.game import ActionRequest, RuleOutcome
from app.engines.rules_engine import RulesEngine

from tests.support import bound_world, village_world


def test_every_registered_action_has_a_rule_method():
    engine = RulesEngine()
    for spec in all_actions():
        assert hasattr(engine, spec.handler), f"{spec.name} points at a missing rule"


def test_registry_matches_the_request_contract():
    allowed = set(ActionRequest.model_fields["action"].annotation.__args__)
    assert set(action_names()) == allowed, "the API contract and the catalogue drifted"


def test_unknown_action_is_rejected_with_a_readable_code():
    world = village_world()
    state = None
    from app.services.world_service import build_initial_state

    state = build_initial_state(world, "camp-1")
    outcome = RulesEngine().evaluate(world, state, "teleport")
    assert outcome.ok is False
    assert outcome.code == "action_not_supported"
    assert "teleport" in outcome.message


def test_a_registered_action_dispatches_to_its_handler(monkeypatch):
    calls = []

    def fake(self, world, state, target_id=None, params=None):
        calls.append((target_id, params))
        return RuleOutcome(ok=True, code="poked", message="poked")

    monkeypatch.setattr(RulesEngine, "_look", fake)
    world = village_world()
    from app.services.world_service import build_initial_state

    state = build_initial_state(world, "camp-1")
    outcome = RulesEngine().evaluate(world, state, "look")
    assert outcome.code == "poked"
    assert calls == [(None, {})]


def test_catalogue_exposes_target_requirements():
    look = get_action("look")
    take = get_action("take")
    assert look is not None and look.target == "none"
    assert take is not None and take.target == "required"
    payload = take.as_dict()
    assert payload["name"] == "take" and payload["target"] == "required"
    assert "拿" in payload["verbs"]


def test_verb_table_keeps_the_authored_order():
    table = dict(verb_table())
    # Chinese verbs cannot be ordered by character count, so the declaration
    # order is kept verbatim: "打开" must be tried before the bare "开".
    assert table["open"].index("打开") < table["open"].index("开")
    assert table["take"].index("拾取") < table["take"].index("拿")


def test_registering_an_action_is_idempotent_and_reversible(monkeypatch):
    spec = ActionSpec(name="sing", label="唱歌", handler="_look", verbs=("唱歌",))
    register_action(spec)
    try:
        assert "sing" in action_names()
        assert action_names().count("sing") == 1
        register_action(spec)
        assert action_names().count("sing") == 1
        assert "sing" in dict(verb_table())
    finally:
        from app.engines import action_registry

        action_registry._SPECS.pop("sing", None)
        action_registry._ORDER.remove("sing")
    assert "sing" not in action_names()
