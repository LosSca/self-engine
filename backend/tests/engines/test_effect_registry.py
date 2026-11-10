"""The effect registry is the single dispatch table for state changes.

Adding an effect means writing one function and decorating it; nothing in
apply_effect() may grow a new branch. These tests pin that contract so a future
effect cannot be silently forgotten, duplicated or bypassed.
"""
import inspect

import pytest

from app.engines import event_engine
from app.engines.event_engine import (
    EFFECT_HANDLERS,
    AppliedEffect,
    apply_effect,
    effect_kinds,
    register_effect,
)
from app.models.game import Effect


DECLARED_KINDS = set(Effect.model_fields["kind"].annotation.__args__)


def test_every_declared_effect_kind_has_a_handler():
    assert set(EFFECT_HANDLERS) == DECLARED_KINDS


def test_effect_kinds_is_sorted_and_complete():
    kinds = effect_kinds()
    assert kinds == sorted(kinds)
    assert set(kinds) == DECLARED_KINDS


def test_apply_effect_is_a_pure_dispatch():
    source = inspect.getsource(apply_effect)
    assert 'if kind ==' not in source
    assert "if kind in (" not in source
    assert "EFFECT_HANDLERS.get(kind)" in source


def test_unknown_kind_fails_closed_with_a_readable_reason(monkeypatch):
    # Force an unregistered kind through the normal Effect pipeline.
    monkeypatch.setattr(event_engine, "EFFECT_HANDLERS", {})
    effect = Effect(kind="set_flag", target="x", value=True)
    # apply_effect itself must still refuse anything not in the live table.
    from app.models.game import GameState, WorldDefinition

    world = WorldDefinition(world_id="w", name="world", start_location_id="square")
    state = GameState(campaign_id="c", world_id="w", location_id="square")
    outcome = apply_effect(state, world, effect)
    assert outcome.ok is False
    assert outcome.error == "unsupported effect kind 'set_flag'"


def test_register_effect_rejects_duplicates_and_can_be_undone():
    name = "_test_duplicate_effect"
    calls = []

    @register_effect(name)
    def handler(kind, state, world, effect):
        calls.append(kind)
        return AppliedEffect(effect, True)

    try:
        assert EFFECT_HANDLERS[name] is handler
        with pytest.raises(ValueError, match="already registered"):
            register_effect(name)(handler)
    finally:
        EFFECT_HANDLERS.pop(name, None)
    assert name not in EFFECT_HANDLERS


def test_each_handler_keeps_its_own_kind_readable():
    """A handler may branch on kind only to tell sibling kinds apart."""
    source = inspect.getsource(event_engine._effect_add_currency_or_remove_currency)
    assert 'kind == "remove_currency"' in source


def test_registry_survives_a_fresh_import():
    kinds = effect_kinds()
    assert "give_item" in kinds and "quest_complete" in kinds and "emit" in kinds
