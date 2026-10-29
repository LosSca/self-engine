"""Optimistic concurrency: a slow writer must never overwrite a fast one."""
import asyncio

import pytest

from app.db.game_state_store import GameStateStore, StateConflictError
from app.models.game import ActionRequest, GameState
from app.services.action_service import ActionService
from app.services.narrative_service import TemplateNarrator

WORLD_ID = "conc_world"


@pytest.fixture
def store(tmp_path):
    store = GameStateStore(str(tmp_path / "game_state.db"))
    yield store
    store.close()


@pytest.fixture
def world():
    from app.models.game import ItemDef, LocationDef, WorldDefinition

    return WorldDefinition(
        world_id=WORLD_ID,
        name="Concurrency Yard",
        start_location_id="yard",
        locations=[
            LocationDef(id="yard", name="Yard", item_ids=["k1", "k2"]),
            LocationDef(id="gate", name="Gate"),
        ],
        items=[ItemDef(id="k1", name="Key One"), ItemDef(id="k2", name="Key Two")],
    )


def _bind(store, world, campaign_id="camp-conc"):
    from app.services.world_service import build_initial_state

    store.save_world(world)
    state = build_initial_state(world, campaign_id)
    state.item_locations = {"k1": "yard", "k2": "yard"}
    store.bind_campaign(state)
    return state


def _commit(store, state, request_id, version, flag_value):
    new_state = state.model_copy(deep=True)
    new_state.version = version
    new_state.flags["marker"] = flag_value
    store.commit_action(
        campaign_id=state.campaign_id,
        request_id=request_id,
        request_payload={},
        status="applied",
        result_payload={"request_id": request_id, "campaign_id": state.campaign_id},
        state=new_state,
        events=[],
        expected_version=version - 1,
    )


def test_stale_commit_is_rejected_and_rolled_back(store, world):
    state = _bind(store, world)
    _commit(store, state, "req-a", version=1, flag_value="first")

    with pytest.raises(StateConflictError) as excinfo:
        _commit(store, state, "req-b", version=1, flag_value="second")

    assert excinfo.value.expected_version == 0
    assert excinfo.value.actual_version == 1
    after = store.get_state("camp-conc")
    assert after.version == 1
    assert after.flags.get("marker") == "first"
    # The losing transaction also rolled back its own request row.
    assert store.get_request("req-b") is None


def test_commit_without_expected_version_still_works(store, world):
    state = _bind(store, world)
    new_state = state.model_copy(deep=True)
    new_state.version = 1
    store.commit_action(
        campaign_id=state.campaign_id,
        request_id="req-legacy",
        request_payload={},
        status="applied",
        result_payload={"request_id": "req-legacy"},
        state=new_state,
        events=[],
    )
    assert store.get_state("camp-conc").version == 1


def test_concurrent_turns_both_commit_without_lost_update(store, world):
    _bind(store, world)
    service = ActionService(store, narrator=TemplateNarrator())

    async def run():
        return await asyncio.gather(
            service.execute(
                ActionRequest(
                    campaign_id="camp-conc",
                    request_id="q1",
                    action="take",
                    target_id="k1",
                    narrate=False,
                )
            ),
            service.execute(
                ActionRequest(
                    campaign_id="camp-conc",
                    request_id="q2",
                    action="take",
                    target_id="k2",
                    narrate=False,
                )
            ),
        )

    first, second = asyncio.run(run())

    assert {first.status, second.status} == {"applied"}
    assert first.state_version != second.state_version
    final = store.get_state("camp-conc")
    # Neither pickup was lost: the whole point of the compare-and-set.
    assert final.version == 2
    assert final.inventory.get("k1") == 1
    assert final.inventory.get("k2") == 1
    events = store.list_events("camp-conc", limit=20)
    assert [row["event_type"] for row in events].count("action.applied") == 2


def test_conflict_retry_redecides_from_new_state(store, world, monkeypatch):
    """A turn that loses the race is re-decided, not silently dropped."""
    _bind(store, world)
    service = ActionService(store, narrator=TemplateNarrator())
    real_commit = store.commit_action
    state = {"raced": False}

    def racing_commit(**kwargs):
        # First attempt: another request sneaks in and commits before us.
        if not state["raced"]:
            state["raced"] = True
            current = store.get_state("camp-conc")
            sneaky = current.model_copy(deep=True)
            sneaky.version = current.version + 1
            sneaky.flags["sneaky"] = True
            real_commit(
                campaign_id=current.campaign_id,
                request_id="intruder",
                request_payload={},
                status="applied",
                result_payload={"request_id": "intruder"},
                state=sneaky,
                events=[],
                expected_version=current.version,
            )
        return real_commit(**kwargs)

    monkeypatch.setattr(store, "commit_action", racing_commit)

    result = asyncio.run(
        service.execute(
            ActionRequest(
                campaign_id="camp-conc",
                request_id="q-race",
                action="take",
                target_id="k1",
                narrate=False,
            )
        )
    )

    assert result.status == "applied"
    assert "re-resolved after a concurrent change" in result.message
    final = store.get_state("camp-conc")
    assert final.flags.get("sneaky") is True
    assert final.inventory.get("k1") == 1
    assert final.version == 2


def test_conflict_is_reported_when_retry_can_no_longer_apply(store, world, monkeypatch):
    """If the race defeats the retried turn too, the caller gets a conflict."""
    _bind(store, world)
    service = ActionService(store, narrator=TemplateNarrator())

    def always_racing_commit(**kwargs):
        raise StateConflictError("camp-conc", kwargs.get("expected_version", 0), 99)

    monkeypatch.setattr(store, "commit_action", always_racing_commit)
    monkeypatch.setattr(service, "_retry_on_conflict", lambda *a, **k: _none())

    async def _none():
        return None

    result = asyncio.run(
        service.execute(
            ActionRequest(
                campaign_id="camp-conc",
                request_id="q-lost",
                action="take",
                target_id="k1",
                narrate=False,
            )
        )
    )

    assert result.status == "conflict"
    assert result.code == "version_conflict"
    assert result.ok is False
    # Nothing of this request reached storage.
    assert store.get_request("q-lost") is None
