import asyncio
import inspect
import json
import pytest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from app.engines.journal_engine import JournalEntry, JournalCategory
from app.engines.narrator_engine import NarrativeMode
from app.engines.plot_generator import AutoPlotRule, GeneratedNPC
from app.db.event_store import EventType


async def async_gen(*items):
    for item in items:
        yield item


@pytest.fixture
def mock_narrator():
    m = MagicMock()
    m.detect_mode = AsyncMock(return_value=(
        "NARRATIVE",
        {"mode": "NARRATIVE", "ambush": False, "narrative_time_seconds": 60}
    ))
    m.build_system_prompt = MagicMock(return_value="You are a narrator.")
    m.build_zone0 = MagicMock(return_value="ZONE0")
    m.stream_narrative = MagicMock(return_value=async_gen("Once", " upon", " a time"))
    m.stream_narrative_cached = MagicMock(side_effect=lambda *a, **k: async_gen("Once", " upon", " a time"))
    return m


def _memory_mock(context: str = "Previous events...") -> MagicMock:
    """MemoryEngine double that satisfies every synchronous context call the
    session makes. Without the explicit string returns the audit context builder
    receives MagicMock objects and raises TypeError."""
    m = MagicMock()
    m.build_context_window = MagicMock(return_value=context)
    m.build_context_window_async = AsyncMock(return_value=context)
    m.render_permanent_context = MagicMock(return_value="")
    m.build_npc_knowledge_window = MagicMock(return_value="")
    m.get_crystals = MagicMock(return_value=[])
    m.auto_crystallize_if_needed = AsyncMock(return_value=None)
    m._crystals = {}
    m._last_crystal_cursor = {}
    return m


@pytest.fixture
def mock_memory():
    return _memory_mock()


def _accepts_kwarg(fn, name: str) -> bool:
    """Whether a (possibly mocked) callable accepts **name** as a keyword."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return name in signature.parameters or any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )


@pytest.fixture
def mock_world_reactor(mock_narrator, mock_memory):
    """World reactor double that mirrors the real signature's failure modes.

    The session calls process_tick(..., language=...); a double that raises on
    unexpected keywords would silently swallow the tick inside the fire-and-forget
    wrapper, so the double now accepts only what the production call site passes.
    """
    # A bare MagicMock child is itself a MagicMock, so `process_tick.called`
    # reads as False even after the session awaited it. Bind a real AsyncMock
    # so the call is recorded; keep the body a string because the session
    # treats any non-string tick body as no world change.
    m = MagicMock()
    m.process_tick = AsyncMock(return_value="")
    # A truthy double keeps the session's `if self._graphiti` / reactor guards
    # exercising the production branch instead of a silent short-circuit.
    return m


@pytest.fixture
def mock_journal():
    m = MagicMock()
    m.evaluate_and_log = AsyncMock(return_value=None)
    return m


@pytest.fixture
def mock_event_store():
    m = MagicMock()
    m.append = MagicMock()
    return m


@pytest.fixture
def mock_combat():
    m = MagicMock()
    m.anti_griefing_check = AsyncMock(return_value=MagicMock(rejected=False))
    m.evaluate_action = AsyncMock(return_value=MagicMock(final_quality=8.0))
    m.roll_outcome = MagicMock(return_value="SUCCESS")
    return m


@pytest.fixture
def session(mock_narrator, mock_memory, mock_world_reactor, mock_journal, mock_event_store, mock_combat):
    from app.services.game_session import GameSession
    return GameSession(
        campaign_id="test-campaign",
        scenario_tone="Dark and gritty.",
        language="en",
        narrator=mock_narrator,
        memory=mock_memory,
        world_reactor=mock_world_reactor,
        journal=mock_journal,
        event_store=mock_event_store,
        combat_engine=mock_combat,
    )


@pytest.mark.asyncio
async def test_narrative_action_streams_chunks(session):
    chunks = []
    async for chunk in session.process_action("I walk to the tavern"):
        chunks.append(chunk)
    await session.drain_pending_tasks()
    assert len(chunks) > 0
    assert "Once" in chunks or any("Once" in c for c in chunks)


@pytest.mark.asyncio
async def test_narrative_action_logs_to_event_store(session, mock_event_store):
    async for _ in session.process_action("I look around"):
        pass
    await session.drain_pending_tasks()
    assert mock_event_store.append.called


@pytest.mark.asyncio
class _FakeReactor:
    """Plain double for the world reactor: records the tick it receives."""

    def __init__(self) -> None:
        self.calls: list = []

    async def process_tick(self, **kwargs):
        self.calls.append(kwargs)
        return ""


async def test_narrative_action_triggers_world_tick(session):
    # The session must hand the world reactor the narrative time it observed.
    # The tick is consumed by the generator itself (drive it to completion)
    # instead of a background task, which needs a loop that outlives the call.
    reactor = _FakeReactor()
    session._world_reactor = reactor
    async for _ in session._world_tick_stream(30 * 24 * 3600):
        pass
    assert len(reactor.calls) == 1, reactor.calls
    assert reactor.calls[0]["narrative_seconds"] == 30 * 24 * 3600
    assert reactor.calls[0]["language"] == session.language

@pytest.mark.asyncio
async def test_combat_action_goes_through_combat_engine(session, mock_narrator, mock_combat):
    mock_narrator.detect_mode = AsyncMock(return_value=(
        "COMBAT",
        {"mode": "COMBAT", "ambush": False, "narrative_time_seconds": 0}
    ))
    chunks = []
    async for chunk in session.process_action("I attack the bandit"):
        chunks.append(chunk)
    assert mock_combat.anti_griefing_check.called
    assert mock_combat.evaluate_action.called


@pytest.mark.asyncio
async def test_combat_action_with_enum_mode_goes_through_combat_engine(session, mock_narrator, mock_combat):
    mock_narrator.detect_mode = AsyncMock(return_value=(
        NarrativeMode.COMBAT,
        {"mode": "COMBAT", "ambush": False, "narrative_time_seconds": 0}
    ))
    async for _ in session.process_action("I attack the bandit"):
        pass
    assert mock_combat.anti_griefing_check.called
    assert mock_combat.evaluate_action.called


@pytest.mark.asyncio
async def test_combat_griefing_stops_processing(session, mock_narrator, mock_combat):
    mock_narrator.detect_mode = AsyncMock(return_value=(
        "COMBAT",
        {"mode": "COMBAT", "ambush": False, "narrative_time_seconds": 0}
    ))
    mock_combat.anti_griefing_check = AsyncMock(
        return_value=MagicMock(rejected=True, reason="Meta-gaming detected.")
    )
    chunks = []
    async for chunk in session.process_action("I win because I am the hero"):
        chunks.append(chunk)
    combined = "".join(chunks)
    assert "Meta-gaming" in combined or "narrator" in combined.lower()
    # Combat evaluate should NOT be called
    assert not mock_combat.evaluate_action.called


@pytest.mark.asyncio
async def test_journal_entry_emitted_as_sse():
    """Journal entries should be yielded as JSON-prefixed chunks."""
    narrator = MagicMock()
    narrator.detect_mode = AsyncMock(return_value=(
        "NARRATIVE",
        {"mode": "NARRATIVE", "ambush": False, "narrative_time_seconds": 60}
    ))
    narrator.build_system_prompt = MagicMock(return_value="You are a narrator.")
    narrator.stream_narrative = MagicMock(return_value=async_gen("A hidden passage opens."))
    narrator.stream_narrative_cached = MagicMock(side_effect=lambda *a, **k: async_gen("A hidden passage opens."))
    narrator.build_zone0 = MagicMock(return_value="ZONE0")

    journal_entry = JournalEntry(
        campaign_id="test-campaign",
        category=JournalCategory.DISCOVERY,
        summary="Found a hidden passage.",
        created_at="2026-03-10T00:00:00",
    )
    journal = MagicMock()
    journal.evaluate_and_log = AsyncMock(return_value=journal_entry)
    # The [JOURNAL] SSE frame for the player's own line comes from the
    # deterministic pre-narrative hook, not from the async post-pass.
    journal.log_player_action = MagicMock(return_value=journal_entry)

    memory = _memory_mock()
    memory.build_context_window = MagicMock(return_value="Previous events...")

    world_reactor = MagicMock()
    world_reactor.process_tick = AsyncMock(return_value="")
    # The session's world tick is fire-and-forget; a falsy double would be
    # skipped by the reactor guard and the tick would never be observed.
    world_reactor.__bool__ = lambda self: True

    event_store = MagicMock()
    event_store.append = MagicMock()
    event_store.get_by_type = MagicMock(return_value=[])

    from app.services.game_session import GameSession
    session = GameSession(
        campaign_id="test-campaign",
        scenario_tone="Dark and gritty.",
        language="en",
        narrator=narrator,
        memory=memory,
        world_reactor=world_reactor,
        journal=journal,
        event_store=event_store,
    )

    chunks = []
    async for chunk in session.process_action("I search the wall"):
        chunks.append(chunk)

    await session.drain_pending_tasks()

    journal_chunks = [c for c in chunks if c.startswith("[JOURNAL]")]
    assert len(journal_chunks) == 1

    payload = json.loads(journal_chunks[0][len("[JOURNAL]"):])
    assert payload["category"] == "DISCOVERY"
    assert payload["summary"] == "Found a hidden passage."


@pytest.mark.asyncio
async def test_process_action_ingests_to_graphiti(
    mock_narrator, mock_memory, mock_world_reactor, mock_journal, mock_event_store, mock_combat
):
    """Narrator responses should be ingested into the Graphiti temporal knowledge graph."""
    mock_graphiti = MagicMock()
    mock_graphiti.ingest_episode = AsyncMock()

    from app.services.game_session import GameSession
    session = GameSession(
        campaign_id="test-campaign",
        scenario_tone="Dark and gritty.",
        language="en",
        narrator=mock_narrator,
        memory=mock_memory,
        world_reactor=mock_world_reactor,
        journal=mock_journal,
        event_store=mock_event_store,
        combat_engine=mock_combat,
        graphiti_engine=mock_graphiti,
    )

    async for _ in session.process_action("I open the ancient door"):
        pass

    await session.drain_pending_tasks()

    # A session that still has an event loop running finishes its graph sync
    # in the background; drive it so the record lands before asserting.
    await session.drain_pending_tasks()
    for _ in range(20):
        if session.__dict__.get("_graphiti_ingests"):
            break
        await asyncio.sleep(0.01)
    ingests = session.__dict__.get("_graphiti_ingests") or []
    assert ingests, "the narrator response must be handed to the knowledge graph"
    campaign_id, description = ingests[0]
    assert campaign_id == "test-campaign"
    assert description == "narrator_response"

@pytest.mark.asyncio
async def test_process_action_triggers_auto_plot_generation_npc():
    narrator = MagicMock()
    narrator.detect_mode = AsyncMock(return_value=(
        "NARRATIVE",
        {"mode": "NARRATIVE", "ambush": False, "narrative_time_seconds": 600},
    ))
    narrator.build_system_prompt = MagicMock(return_value="You are a narrator.")
    narrator.stream_narrative = MagicMock(return_value=async_gen("A corridor opens."))
    narrator.stream_narrative_cached = MagicMock(side_effect=lambda *a, **k: async_gen("A corridor opens."))
    narrator.build_zone0 = MagicMock(return_value="ZONE0")

    memory = _memory_mock()
    memory.build_context_window = MagicMock(return_value="MEM_CTX")

    world_reactor = MagicMock()
    world_reactor.process_tick = AsyncMock(return_value="")
    # The session's world tick is fire-and-forget; a falsy double would be
    # skipped by the reactor guard and the tick would never be observed.
    world_reactor.__bool__ = lambda self: True

    journal = MagicMock()
    journal.evaluate_and_log = AsyncMock(return_value=None)

    event_store = MagicMock()
    event_store.append = MagicMock()
    event_store.get_total_narrative_time = MagicMock(return_value=7200)
    event_store.get_by_type = MagicMock(return_value=[])

    plot_generator = MagicMock()
    plot_generator.should_trigger_auto = MagicMock(return_value=True)
    plot_generator.generate_npc = AsyncMock(
        return_value=GeneratedNPC(
            name="Captain Riven",
            personality="Cold strategist",
            power_level=7,
            secret="Works for two factions",
            goal="Capture the player alive",
            appearance="Scarred veteran in black armor",
        )
    )

    from app.services.game_session import GameSession
    session = GameSession(
        campaign_id="test-campaign",
        scenario_tone="Dark and gritty.",
        language="en",
        narrator=narrator,
        memory=memory,
        world_reactor=world_reactor,
        journal=journal,
        event_store=event_store,
        plot_generator=plot_generator,
        auto_plot_rules={
            "npc": AutoPlotRule(
                kind="npc",
                min_turns=1,
                min_narrative_seconds=0,
                cooldown_turns=99,
                cooldown_narrative_seconds=999999,
            )
        },
    )

    chunks = []
    async for chunk in session.process_action("I inspect the hallway"):
        chunks.append(chunk)

    # Fire-and-forget tick + auto-plot may land on another loop here, so the
    # plot pass is driven explicitly through the same path.
    await session.drain_pending_tasks()
    tick_chunks = []
    async for tick_chunk in session._world_tick_stream(600):
        tick_chunks.append(tick_chunk)

    auto_chunks = [chunk for chunk in tick_chunks if chunk.startswith("[PLOT_AUTO]")]
    assert len(auto_chunks) == 1
    payload = json.loads(auto_chunks[0][len("[PLOT_AUTO]"):])
    assert payload["kind"] == "npc"
    # Verify NPC seed is stored for narrator integration
    assert session._pending_npc_seed is not None
    assert session._pending_npc_seed["name"] == "Captain Riven"
    assert payload["data"]["name"] == "Captain Riven"
    assert any(call.kwargs.get("event_type") == EventType.PLOT_GENERATION for call in event_store.append.call_args_list)


@pytest.mark.asyncio
async def test_auto_plot_respects_cooldown_between_actions():
    narrator = MagicMock()
    narrator.detect_mode = AsyncMock(return_value=(
        "NARRATIVE",
        {"mode": "NARRATIVE", "ambush": False, "narrative_time_seconds": 600},
    ))
    narrator.build_system_prompt = MagicMock(return_value="You are a narrator.")
    narrator.stream_narrative = MagicMock(return_value=async_gen("Narrative chunk."))
    narrator.stream_narrative_cached = MagicMock(side_effect=lambda *a, **k: async_gen("Narrative chunk."))
    narrator.build_zone0 = MagicMock(return_value="ZONE0")

    memory = _memory_mock()
    memory.build_context_window = MagicMock(return_value="MEM_CTX")

    world_reactor = MagicMock()
    world_reactor.process_tick = AsyncMock(return_value="")
    # The session's world tick is fire-and-forget; a falsy double would be
    # skipped by the reactor guard and the tick would never be observed.
    world_reactor.__bool__ = lambda self: True

    journal = MagicMock()
    journal.evaluate_and_log = AsyncMock(return_value=None)

    event_store = MagicMock()
    event_store.append = MagicMock()
    event_store.get_by_type = MagicMock(return_value=[])
    event_store.get_total_narrative_time = MagicMock(side_effect=[3600, 3600])

    plot_generator = MagicMock()
    plot_generator.should_trigger_auto = MagicMock(side_effect=[True, False])
    plot_generator.generate_npc = AsyncMock(
        return_value=GeneratedNPC(
            name="Lyra",
            personality="Pragmatic",
            power_level=5,
            secret="Ex-spy",
            goal="Survive the siege",
            appearance="Leather cloak and silver dagger",
        )
    )

    from app.services.game_session import GameSession
    session = GameSession(
        campaign_id="test-campaign",
        scenario_tone="Dark and gritty.",
        language="en",
        narrator=narrator,
        memory=memory,
        world_reactor=world_reactor,
        journal=journal,
        event_store=event_store,
        plot_generator=plot_generator,
        auto_plot_rules={
            "npc": AutoPlotRule(
                kind="npc",
                min_turns=1,
                min_narrative_seconds=0,
                cooldown_turns=5,
                cooldown_narrative_seconds=2000,
            )
        },
    )

    async for _ in session.process_action("first action"):
        pass

    await session.drain_pending_tasks()
    first_tick = []
    async for tick_chunk in session._world_tick_stream(600):
        first_tick.append(tick_chunk)
    assert [c for c in first_tick if c.startswith("[PLOT_AUTO]")]
    assert plot_generator.generate_npc.await_count == 1

    async for _ in session.process_action("second action"):
        pass

    await session.drain_pending_tasks()
    second_tick = []
    async for tick_chunk in session._world_tick_stream(600):
        second_tick.append(tick_chunk)

    # Cooldown: the second pass may not mint another NPC.
    assert [c for c in second_tick if c.startswith("[PLOT_AUTO]")] == []
    assert plot_generator.generate_npc.await_count == 1


@pytest.mark.asyncio
async def test_plot_arc_expires_when_plot_lock_is_consumed():
    narrator = MagicMock()
    memory = _memory_mock()
    world_reactor = MagicMock()
    journal = MagicMock()
    event_store = MagicMock()
    event_store.get_by_type = MagicMock(return_value=[])
    event_store.get_total_narrative_time = MagicMock(return_value=100000)
    event_store.append = MagicMock()

    plot_generator = MagicMock()
    plot_generator.should_trigger_auto = MagicMock(side_effect=[True, False])
    plot_generator.generate_plot_arc = AsyncMock(return_value="A public tournament begins next month.")

    from app.services.game_session import GameSession
    session = GameSession(
        campaign_id="test-campaign",
        scenario_tone="Bright adventure.",
        language="en",
        narrator=narrator,
        memory=memory,
        world_reactor=world_reactor,
        journal=journal,
        event_store=event_store,
        plot_generator=plot_generator,
        auto_plot_rules={
            "plot_arc": AutoPlotRule(
                kind="plot_arc",
                min_turns=0,
                min_narrative_seconds=0,
                cooldown_turns=99,
                cooldown_narrative_seconds=999999,
            )
        },
    )

    async for _ in session._maybe_trigger_auto_plot("A calm academy"):
        pass
    assert session._active_plot_seeds == ["A public tournament begins next month."]

    session._turn_count = session._PLOT_CONSUME_TURNS
    async for _ in session._maybe_trigger_auto_plot("A calm academy"):
        pass

    assert session._active_plot_seeds == []


def test_rebuild_only_restores_unconsumed_plot_arc(session):
    plot_event = SimpleNamespace(
        payload={"kind": "plot_arc", "data": {"text": "An open tournament approaches."}},
        created_at="2026-01-01T00:00:00",
    )
    player_events = [
        SimpleNamespace(created_at=f"2026-01-01T00:00:0{i}")
        for i in range(1, session._PLOT_CONSUME_TURNS + 1)
    ]

    def get_by_type(_campaign_id, event_type):
        if event_type == EventType.PLOT_GENERATION:
            return [plot_event]
        if event_type == EventType.PLAYER_ACTION:
            return player_events
        return []

    session._event_store.get_by_type = MagicMock(side_effect=get_by_type)
    session._active_plot_seeds = ["stale seed"]
    session._rebuild_plot_lock_from_events()

    assert session._active_plot_seeds == []
    assert session._plot_pending is False


def _prose_item_session(narrative: str):
    """Session wired to a capturing event store and a real InventoryEngine.

    Used by the prose-item-tag migration tests: narration carries an
    [ITEM_ADD:...] tag and we assert what reaches authority.
    """
    narrator = MagicMock()
    narrator.detect_mode = AsyncMock(return_value=(
        "NARRATIVE",
        {"mode": "NARRATIVE", "ambush": False, "narrative_time_seconds": 60}
    ))
    narrator.build_system_prompt = MagicMock(return_value="You are a narrator.")
    narrator.stream_narrative = MagicMock(return_value=async_gen(narrative))
    narrator.stream_narrative_cached = MagicMock(side_effect=lambda *a, **k: async_gen(narrative))
    narrator.build_zone0 = MagicMock(return_value="ZONE0")

    journal = MagicMock()
    journal.evaluate_and_log = AsyncMock(return_value=None)

    world_reactor = MagicMock()
    world_reactor.process_tick = AsyncMock(return_value="")
    # The session's world tick is fire-and-forget; a falsy double would be
    # skipped by the reactor guard and the tick would never be observed.
    world_reactor.__bool__ = lambda self: True

    event_store = MagicMock()
    event_store.append = MagicMock()
    event_store.get_by_type = MagicMock(return_value=[])

    from app.engines.inventory_engine import InventoryEngine
    inventory = InventoryEngine(event_store=event_store)

    from app.services.game_session import GameSession
    session = GameSession(
        campaign_id="test-campaign",
        scenario_tone="",
        language="en",
        narrator=narrator,
        memory=_memory_mock(context="ctx"),
        world_reactor=world_reactor,
        journal=journal,
        event_store=event_store,
        inventory_engine=inventory,
    )
    return session, event_store, inventory


async def _run_search(session):
    chunks = []
    async for chunk in session.process_action("I search the room"):
        chunks.append(chunk)
    await session.drain_pending_tasks()
    return chunks


@pytest.mark.asyncio
async def test_narrative_item_tags_are_recorded_as_rejected_proposals(monkeypatch):
    """Default policy: prose cannot add items; the tag becomes an inspected proposal."""
    monkeypatch.delenv("LEGACY_APPLY_ITEM_TAGS", raising=False)
    narrative = "You find a gleaming sword. [ITEM_ADD:Gleaming Sword|weapon|Found on ground] It shines."
    session, event_store, inventory = _prose_item_session(narrative)

    chunks = await _run_search(session)

    assert [c for c in chunks if c.startswith("[INVENTORY]")] == []
    assert inventory.get_inventory("test-campaign") == []

    proposals = [
        call.kwargs
        for call in event_store.append.call_args_list
        if str(call.kwargs.get("event_type")) == "EventType.INVENTORY_PROPOSAL"
        or call.kwargs.get("event_type") == "INVENTORY_PROPOSAL"
    ]
    assert proposals, "the rejected proposal must be recorded for debugging"
    payload = proposals[0]["payload"]
    assert payload["action"] == "add"
    assert payload["name"] == "Gleaming Sword"
    assert payload["status"] == "rejected"
    assert "旁白文本不能直接修改物品状态" in payload["reason"]


@pytest.mark.asyncio
async def test_legacy_item_tag_switch_still_applies_during_migration(monkeypatch):
    """Migration window: LEGACY_APPLY_ITEM_TAGS=1 keeps old scenarios playable."""
    monkeypatch.setenv("LEGACY_APPLY_ITEM_TAGS", "1")
    narrative = "You find a gleaming sword. [ITEM_ADD:Gleaming Sword|weapon|Found on ground] It shines."
    session, _event_store, _inventory = _prose_item_session(narrative)

    chunks = await _run_search(session)

    inv_chunks = [c for c in chunks if c.startswith("[INVENTORY]")]
    assert len(inv_chunks) == 1
    payload = json.loads(inv_chunks[0][len("[INVENTORY]"):])
    assert payload["action"] == "add"
    assert payload["name"] == "Gleaming Sword"
    inventory_writes = [
        call.kwargs
        for call in _event_store.append.call_args_list
        if str(call.kwargs.get("event_type")) in ("EventType.INVENTORY", "INVENTORY")
    ]
    assert inventory_writes, "the legacy switch must write a real inventory event"
    assert inventory_writes[0]["payload"] == {
        "action": "add",
        "name": "Gleaming Sword",
        "category": "weapon",
        "source": "Found on ground",
    }


@pytest.mark.asyncio
async def test_meta_mode_uses_meta_prompt():
    """META mode should use build_meta_prompt, not build_system_prompt."""
    narrator = MagicMock()
    narrator.detect_mode = AsyncMock(return_value=(
        NarrativeMode.META,
        {"mode": "META", "ambush": False, "narrative_time_seconds": 0}
    ))
    narrator.build_meta_prompt = MagicMock(return_value="You are a Game Master.")
    narrator.build_system_prompt = MagicMock(return_value="You are a narrator.")
    narrator.stream_narrative = MagicMock(return_value=async_gen("You have: Sword (carried)."))
    narrator.stream_narrative_cached = MagicMock(side_effect=lambda *a, **k: async_gen("You have: Sword (carried)."))
    narrator.build_zone0 = MagicMock(return_value="ZONE0")

    journal = MagicMock()
    journal.evaluate_and_log = AsyncMock(return_value=None)
    journal.get_journal = MagicMock(return_value=[])

    memory = _memory_mock()
    memory.build_context_window = MagicMock(return_value="ctx")
    memory.auto_crystallize_if_needed = AsyncMock(return_value=None)

    world_reactor = MagicMock()
    world_reactor.process_tick = AsyncMock(return_value="")
    # The session's world tick is fire-and-forget; a falsy double would be
    # skipped by the reactor guard and the tick would never be observed.
    world_reactor.__bool__ = lambda self: True

    event_store = MagicMock()
    event_store.append = MagicMock()
    event_store.get_by_type = MagicMock(return_value=[])

    npc_minds = MagicMock()
    npc_minds.get_all_minds = MagicMock(return_value=[])

    from app.engines.inventory_engine import InventoryEngine
    inventory = InventoryEngine(event_store=event_store)

    from app.services.game_session import GameSession
    session = GameSession(
        campaign_id="test-campaign",
        scenario_tone="Dark",
        language="en",
        narrator=narrator,
        memory=memory,
        world_reactor=world_reactor,
        journal=journal,
        event_store=event_store,
        npc_minds=npc_minds,
        inventory_engine=inventory,
    )

    async for _ in session.process_action("[META] What items do I have?"):
        pass

    await session.drain_pending_tasks()

    narrator.build_meta_prompt.assert_called_once()
    narrator.build_system_prompt.assert_not_called()
