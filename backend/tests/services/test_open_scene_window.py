import json
import uuid

from unittest.mock import MagicMock

from app.engines.memory_engine import CrystalTier
from app.engines.narrator_engine import NarratorEngine, estimate_tokens
from app.db.event_store import EventStore, EventType


def _crystal(tier, end):
    c = MagicMock()
    c.tier = tier
    c.source_end_created_at = end
    return c


def _short(end):
    return _crystal(CrystalTier.SHORT, end)


def _make_session(history):
    from app.services.game_session import GameSession
    s = GameSession(
        campaign_id="c1",
        scenario_tone="",
        language="en",
        narrator=MagicMock(),
        memory=MagicMock(),
        world_reactor=MagicMock(),
        journal=MagicMock(),
        event_store=MagicMock(),
    )
    s._history = list(history)
    return s


def _hist(n):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(n)
    ]


def test_trims_to_open_scene_and_uses_overlap_cursor():
    hist = _hist(20)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[_short("t1"), _short("t2"), _short("t3")])
    s._event_store.get_after = MagicMock(return_value=[object()] * 6)

    window = s._open_scene_history()

    assert window == hist[-6:]
    # overlap = one batch back → second-to-last SHORT crystal boundary
    assert s._event_store.get_after.call_args.kwargs["after_created_at"] == "t2"


def test_never_cuts_before_the_cursor():
    """Invariant: the window is exactly the tail of the events past the overlap cursor."""
    hist = _hist(30)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[_short("a"), _short("b"), _short("c"), _short("d")])
    for n in (5, 8, 12):
        s._event_store.get_after = MagicMock(return_value=[object()] * n)
        assert s._open_scene_history() == hist[-n:]


def test_coherence_floor_of_four_messages():
    hist = _hist(20)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[_short("t1"), _short("t2")])
    s._event_store.get_after = MagicMock(return_value=[object()] * 2)  # n=2 < floor

    assert s._open_scene_history() == hist[-4:]


def test_early_game_keeps_full_history():
    hist = _hist(6)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[_short("t1")])  # <2 SHORT crystals

    assert s._open_scene_history() == hist
    s._event_store.get_after.assert_not_called()


def test_no_short_crystals_keeps_full_history():
    hist = _hist(6)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[])

    assert s._open_scene_history() == hist


def test_missing_cursor_keeps_full_history():
    hist = _hist(10)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[_short(None), _short(None), _short(None)])

    assert s._open_scene_history() == hist


def test_pathological_backlog_falls_back_to_full_history():
    hist = _hist(20)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[_short("t1"), _short("t2"), _short("t3")])
    s._event_store.get_after = MagicMock(return_value=[object()] * s._OPEN_SCENE_MAX_EVENTS)

    assert s._open_scene_history() == hist


def test_no_open_events_falls_back_to_full_history():
    hist = _hist(20)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[_short("t1"), _short("t2"), _short("t3")])
    s._event_store.get_after = MagicMock(return_value=[])

    assert s._open_scene_history() == hist


def test_computation_error_falls_back_to_full_history():
    hist = _hist(20)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(side_effect=RuntimeError("boom"))

    assert s._open_scene_history() == hist


def test_flag_off_keeps_full_history(monkeypatch):
    monkeypatch.setenv("LUNAR_FEATURE_OPEN_SCENE_WINDOW", "0")
    hist = _hist(20)
    s = _make_session(hist)
    s._memory.get_crystals = MagicMock(return_value=[_short("t1"), _short("t2"), _short("t3")])

    assert s._open_scene_history() == hist
    s._memory.get_crystals.assert_not_called()


def test_token_savings_on_a_long_campaign(capsys):
    """Quantify the FASE 1 win: history tokens sent to the narrator collapse from
    O(campaign) to O(open scene). Mirrors the FASE 0 baseline (~133k history tokens)."""
    # 300 exchanges of realistic prose: short player line + long narrator beat.
    exchanges = 300
    hist = []
    for i in range(exchanges):
        hist.append({"role": "user", "content": f"I do a thing number {i}. " * 4})       # ~100 chars
        hist.append({"role": "assistant", "content": f"The scene unfolds ({i}). " * 90})  # ~2000 chars

    s = _make_session(hist)
    # Open scene = last 5 exchanges (10 messages) past the crystal boundary.
    s._memory.get_crystals = MagicMock(return_value=[_short("t1"), _short("t2"), _short("t3")])
    s._event_store.get_after = MagicMock(return_value=[object()] * 10)

    cw = 1_000_000  # baseline provider window
    sys_tokens = 20_000  # crystals + story cards + tone, unchanged by FASE 1

    old_slice = NarratorEngine._dynamic_history_slice(hist, cw, sys_tokens)
    new_slice = NarratorEngine._dynamic_history_slice(s._open_scene_history(), cw, sys_tokens)

    old_tok = sum(estimate_tokens(m["content"]) for m in old_slice)
    new_tok = sum(estimate_tokens(m["content"]) for m in new_slice)
    reduction = 1 - new_tok / old_tok

    with capsys.disabled():
        print(
            f"\n[FASE 1] history tokens to narrator: OLD={old_tok:,} "
            f"({len(old_slice)} msgs) → NEW={new_tok:,} ({len(new_slice)} msgs) "
            f"= -{reduction*100:.1f}%"
        )

    assert new_tok < old_tok * 0.15  # at least an 85% cut
    assert len(new_slice) == 10


def _insert_event(store, cid, etype, text, created_at):
    """Insert an event with a controlled created_at (utcnow() would collide in a tight loop)."""
    store._conn.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), cid, etype.value, json.dumps({"text": text}),
         0, "loc", json.dumps([]), created_at, json.dumps([])),
    )
    store._conn.commit()


def test_rebuild_restores_true_cursor_not_persist_time(tmp_path):
    """Regression (adversarial finding): a crystal persisted LATER than the raw
    events it covers must rebuild to the true raw boundary, so events created
    before its persist time are not orphaned forever (and stay inside the window)."""
    from app.engines.memory_engine import MemoryEngine
    from app.services.game_session import GameSession

    store = EventStore(str(tmp_path / "e.db"))
    try:
        mem = MemoryEngine(event_store=store, llm=MagicMock())
        cid = "c1"

        ts = lambda s: f"2026-01-01T00:00:{s:02d}"
        for i in range(5):
            _insert_event(store, cid, EventType.PLAYER_ACTION, f"pa{i}", ts(2 * i + 1))
            _insert_event(store, cid, EventType.NARRATOR_RESPONSE, f"nr{i}", ts(2 * i + 2))
        # Batch e1..e8 (through nr3) crystallized; pa4/nr4 are the open orphans.
        batch_end = ts(8)
        # Crystal persisted at :50, after pa4(:09)/nr4(:10). This skew made the
        # boot cursor jump past them before the fix.
        store._conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), cid, EventType.MEMORY_CRYSTAL.value,
             json.dumps({"tier": "SHORT", "summary": "s", "ai_content": "ai",
                         "event_count": 8, "source_start_created_at": ts(1),
                         "source_end_created_at": batch_end, "witnessed_by": []}),
             0, "memory", json.dumps([]), ts(50), json.dumps([])),
        )
        store._conn.commit()

        GameSession(
            campaign_id=cid, scenario_tone="", language="en",
            narrator=MagicMock(), memory=mem, world_reactor=MagicMock(),
            journal=MagicMock(), event_store=store,
        )

        assert mem._last_crystal_cursor[cid] == batch_end   # true raw boundary
        assert mem._last_crystal_cursor[cid] != ts(50)      # not the persist time

        pending = mem._get_uncrystallized_events(cid, limit=100)
        assert [ev.payload.get("text") for ev in pending] == ["pa4", "nr4"]
    finally:
        store.close()
