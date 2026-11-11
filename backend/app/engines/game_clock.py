"""In-game time: the only clock the world has.

The world runs on game time, never on wall time. Time moves when the player
does something that takes time, and it stops the moment the player leaves:
there is no background job, no offline catch-up, and no deferred simulation.
That is the whole Player-Centric rule reduced to one number.

The clock is a pair of counters on the authoritative GameState (day and
minute-of-day) plus a frozen/paused flag kept by the session that owns the
state. Reading it is pure; only `advance` moves it, and only inside the
transaction that commits the player action it belongs to.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

#: World time is a day of 24 buckets of 60 minutes. Minutes are the smallest
#: unit the engine models; anything finer is narration, not state.
MINUTES_PER_HOUR = 60
MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR
DEFAULT_START_DAY = 1
DEFAULT_START_MINUTE = 8 * MINUTES_PER_HOUR

#: How long each verb takes, in minutes. Authored per action in the action
#: catalogue; this table is only the fallback for a verb that forgot to declare
#: one, so a new action can never freeze the clock silently.
DEFAULT_ACTION_MINUTES: Dict[str, int] = {
    "move": 10,
    "look": 0,
    "talk": 0,
    "choose": 0,
    "equip": 1,
    "unequip": 1,
    "investigate": 5,
    "take": 1,
    "open": 1,
    "use": 1,
    "buy": 2,
    "sell": 2,
    "attack": 1,
    "defend": 1,
    "flee": 1,
    "custom": 1,
}
#: A verb that is not in the table at all (a scenario-authored custom action)
#: costs this much rather than nothing: the player did *something*.
UNKNOWN_ACTION_MINUTES = 1

_PARAM_KEYS = ("minutes", "duration_minutes", "duration")
_TIME_WORDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "秒": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "分钟": 60, "分": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
    "小时": 3600, "钟头": 3600,
    "day": 86400, "days": 86400, "天": 86400, "日": 86400,
    "week": 604800, "weeks": 604800, "周": 604800, "星期": 604800,
}
_DURATION_RE = re.compile(r"([0-9]+)[ \t]*([^\s0-9]+)")


def midnight(day: int, minute: int) -> int:
    """Absolute minute index for (day, minute-of-day)."""
    return max(0, int(day or 0)) * MINUTES_PER_DAY + max(0, int(minute or 0))


def split(abs_minute: int) -> tuple[int, int]:
    """(day, minute-of-day) for an absolute minute index; day starts at 0."""
    total = max(0, int(abs_minute or 0))
    return total // MINUTES_PER_DAY, total % MINUTES_PER_DAY


def clock_label(day: int, minute: int) -> str:
    """Human label such as "Day 3, 08:15" — for transcripts, not for parsing."""
    d, m = split(midnight(day, minute))
    return "Day %d, %02d:%02d" % (d, m // MINUTES_PER_HOUR, m % MINUTES_PER_HOUR)


@dataclass(frozen=True)
class Clock:
    """A read-only view of the authoritative clock."""

    day: int = DEFAULT_START_DAY
    minute: int = DEFAULT_START_MINUTE
    paused: bool = False

    @classmethod
    def from_state(cls, state: Any) -> "Clock":
        """Read the clock off a GameState (old saves default to day 1, 08:00)."""
        if state is None:
            return cls()
        paused = bool(getattr(state, "flags", {}) or {}) and False
        return cls(
            day=int(getattr(state, "day", DEFAULT_START_DAY) or 0),
            minute=int(getattr(state, "minute", DEFAULT_START_MINUTE) or 0),
            paused=paused,
        )

    @property
    def abs_minute(self) -> int:
        return midnight(self.day, self.minute)

    @property
    def hour(self) -> int:
        return self.minute // MINUTES_PER_HOUR

    @property
    def time_of_day(self) -> str:
        h = self.hour
        if 5 <= h < 9:
            return "dawn"
        if 9 <= h < 12:
            return "morning"
        if 12 <= h < 14:
            return "midday"
        if 14 <= h < 18:
            return "afternoon"
        if 18 <= h < 22:
            return "evening"
        return "night"

    @property
    def label(self) -> str:
        return clock_label(self.day, self.minute)

    def advance(self, minutes: int) -> "Clock":
        """Return a new clock `minutes` later. Paused clocks do not move."""
        amount = int(minutes or 0)
        if self.paused or amount == 0:
            return self
        day, minute = split(self.abs_minute + amount)
        return replace(self, day=day, minute=minute)

    def pause(self) -> "Clock":
        return replace(self, paused=True)

    def resume(self) -> "Clock":
        return replace(self, paused=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "day": self.day,
            "minute": self.minute,
            "label": self.label,
            "time_of_day": self.time_of_day,
            "paused": self.paused,
            "abs_minute": self.abs_minute,
        }


def duration_minutes(action: str, params: Optional[Dict[str, Any]] = None) -> int:
    """How long this action takes, in minutes.

    Resolution order, most specific first:
    1. an explicit `minutes` / `duration_minutes` / `duration` param,
    2. a `duration` string the player typed ("wait 2 hours", "休息一天"),
    3. the verb default from DEFAULT_ACTION_MINUTES,
    4. UNKNOWN_ACTION_MINUTES for a verb nobody registered.
    """
    params = params or {}
    for key in _PARAM_KEYS:
        raw = params.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, str) and raw.strip().isdigit():
            # A bare number in a string is minutes; anything else (such as
            # "2 hours") goes through the vocabulary below.
            return max(0, int(raw.strip()))
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return max(0, int(raw))
        parsed = parse_duration(str(raw))
        if parsed is not None:
            return parsed
    explicit = params.get("duration_text") or params.get("time")
    if isinstance(explicit, str):
        parsed = parse_duration(explicit)
        if parsed is not None:
            return parsed
    name = (action or "").strip().lower()
    if name in DEFAULT_ACTION_MINUTES:
        return DEFAULT_ACTION_MINUTES[name]
    return UNKNOWN_ACTION_MINUTES


def parse_duration(text: str) -> Optional[int]:
    """Minutes described by free text, or None when the text says nothing about time.

    Only a bounded vocabulary is understood ("2 hours", "30 分钟", "a day");
    anything else returns None so the caller falls back to the verb default.
    Deliberately not a natural-language parser: the clock must stay
    deterministic and testable.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    lowered = text.strip().lower()
    total = 0
    matched = False
    for amount, word in _DURATION_RE.findall(lowered):
        unit = _TIME_WORDS.get(word)
        if unit is None:
            continue
        total += int(amount) * unit
        matched = True
    if not matched:
        return None
    return max(0, total // 60)


def advance_state(state: Any, minutes: int, *, paused: bool = False) -> Any:
    """Move the clock on `state` (a GameState) by `minutes`, in place.

    Returns the resulting Clock. A paused clock never moves, so a caller that
    forgot to resume cannot leak time into a world the player left. The state
    carries `day` / `minute`; saves written before this field existed read as
    day 1, 08:00 through `Clock.from_state`.
    """
    current = Clock.from_state(state)
    if paused:
        current = current.pause()
    moved = current.advance(minutes)
    if hasattr(state, "day"):
        try:
            state.day = moved.day
            state.minute = moved.minute
        except Exception:  # pragma: no cover - frozen models are not ours
            logger.warning("could not write the clock onto %r", type(state).__name__)
    return moved


def schedule_minutes(spec: Any) -> int:
    """Authored duration for an ActionSpec, or its table default."""
    name = getattr(spec, "name", "") or ""
    minutes = getattr(spec, "minutes", None)
    if isinstance(minutes, int) and minutes >= 0:
        return minutes
    return duration_minutes(name)


def is_open_now(opens: int, closes: int, minute: int) -> bool:
    """Whether a shop window covers `minute` of the day.

    Windows that cross midnight (open 22:00, close 02:00) are supported;
    `opens == closes` means "always open", which is how most NPCs behave.
    """
    opens = int(opens or 0) % MINUTES_PER_DAY
    closes = int(closes or 0) % MINUTES_PER_DAY
    minute = int(minute or 0) % MINUTES_PER_DAY
    if opens == closes:
        return True
    if opens < closes:
        return opens <= minute < closes
    return minute >= opens or minute < closes


def active_shift(windows: Iterable[Dict[str, Any]], minute: int) -> Optional[Dict[str, Any]]:
    """The first declared window of the day that contains `minute`, if any.

    This is the entire "NPC daily behaviour" model: a shop opens because the
    clock says so, not because a background process simulated the shopkeeper.
    """
    for window in windows or ():
        if not isinstance(window, dict):
            continue
        if is_open_now(window.get("opens_minute", 0), window.get("closes_minute", 0), minute):
            return window
    return None


def _clock_of(minute_or_clock: Any) -> Any:
    """Accept a Clock, a minute-of-day int, or a GameState."""
    if isinstance(minute_or_clock, Clock):
        return minute_or_clock
    if isinstance(minute_or_clock, int):
        return Clock(day=0, minute=minute_or_clock)
    return Clock.from_state(minute_or_clock)


def open_now(entity: Any, when: Any) -> bool:
    """Whether a merchant entity is open for business at `when`.

    Reads the authored `attributes["hours"]` list ({"opens_minute",
    "closes_minute"}). No hours declared means always open, which is what
    every NPC did before the clock existed.
    """
    attributes = getattr(entity, "attributes", None) or {}
    windows = attributes.get("hours") or attributes.get("opening_hours")
    if not windows:
        return True
    clock = _clock_of(when)
    return bool(active_shift(windows, clock.minute))


def phase_text(clock: Any, language: str = "en") -> str:
    """Short phrase for "it is now <phase>" lines in narration prompts."""
    clock = _clock_of(clock)
    phase = clock.time_of_day
    if (language or "en").lower().startswith("zh"):
        return {
            "dawn": "拂晓", "morning": "上午", "midday": "正午",
            "afternoon": "下午", "evening": "傍晚", "night": "深夜",
        }.get(phase, phase)
    return phase
