"""An LLM that may *suggest* an action, never decide one.

The deterministic parser in app.services.text_action_service handles every
utterance it recognises - verbs, item names, door names, custom action verbs.
When it maps nothing, an advisor can look at the same whitelist the rules use
and return a candidate. The candidate is then handed back to the parser for a
second pass through exactly the same resolution code, so:

* the model only ever picks an action name and a *name fragment*;
* the parser must still find a real target in the world for that fragment;
* the rule engine still decides success, cost and state changes;
* any failure (no model, timeout, garbage output, unknown action, unparsable
  target) degrades to the unchanged `unparsed_action` rejection.

Nothing in this module writes to the store.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from app.engines.action_registry import action_names

logger = logging.getLogger(__name__)

#: Default time budget. An advisor that is slower than this is simply skipped:
#: the deterministic rejection is already a correct answer.
DEFAULT_TIMEOUT = 3.0

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off", "")

_PROMPT = """You map a player's free-form utterance in a text adventure onto one of a fixed list of actions.

Allowed actions (name - what it means - whether a target is needed):
{catalogue}

Reply with ONE json object and nothing else:
{{"action": "<one of the allowed names, or null>", "target": "<the words in the utterance that name the thing acted on, or empty>", "confidence": <0..1>}}

Rules:
- Never invent an action outside the list. If nothing fits, use null.
- Copy the target words from the utterance verbatim; do not translate or rename them.
- Do not describe outcomes. You are not deciding what happens.
"""


def enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Whether the advisor may run at all.

    Default OFF, so a box without a model behaves exactly as before. Turning it
    on is a deliberate switch: `ENGINE_INTENT_ADVISOR=1`.
    """
    import os

    source = env if env is not None else os.environ
    value = str(source.get("ENGINE_INTENT_ADVISOR", "0")).strip().lower()
    if value in _FALSY:
        return False
    return value in _TRUTHY


def timeout_seconds() -> float:
    import os

    raw = os.environ.get("ENGINE_INTENT_TIMEOUT", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(0.5, min(value, 30.0))


@dataclass(frozen=True)
class Suggestion:
    """A candidate the parser still has to make sense of."""

    action: str
    fragment: str = ""
    confidence: float = 0.0


def _catalogue(extra: Sequence[Dict[str, Any]] = ()) -> str:
    lines: List[str] = []
    for name in action_names():
        target = "a target is needed" if name in ("move", "take", "open", "use") else "target optional"
        if name == "custom":
            continue
        lines.append(f"- {name} - {target}")
    for spec in extra:
        lines.append(
            f"- {spec.get('name')} - {spec.get('description') or 'site-specific action'}"
        )
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    raw = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    else:
        match = _JSON_RE.search(raw)
        if match:
            raw = match.group(0)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().strip('"').strip("'")[:120]


def parse_reply(text: str, allowed: Sequence[str]) -> Optional[Suggestion]:
    """Turn a model reply into a suggestion, or None when it cannot be trusted.

    Kept as a pure function so the tests never need a network call.
    """
    data = _extract_json(text)
    if data is None:
        return None
    action = _clean(data.get("action")).lower()
    if action not in allowed:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Suggestion(
        action=action,
        fragment=_clean(data.get("target")),
        confidence=max(0.0, min(confidence, 1.0)),
    )


class IntentAdvisor:
    """Ask a router for a candidate action. Never raises into the request path."""

    def __init__(self, router, timeout: Optional[float] = None):
        self.router = router
        self.timeout = timeout if timeout is not None else timeout_seconds()

    async def suggest(
        self, text: str, world, extra_actions: Sequence[Dict[str, Any]] = ()
    ) -> Optional[Suggestion]:
        if self.router is None or not text:
            return None
        prompt = _PROMPT.format(catalogue=_catalogue(extra_actions))
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text[:400]},
        ]
        try:
            reply = await asyncio.wait_for(
                self.router.complete(messages, temperature=0.0, max_tokens=120),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.info("intent advisor timed out after %.1fs; keeping the rejection", self.timeout)
            return None
        except Exception as exc:  # noqa: BLE001 - degrading is the whole point
            logger.info("intent advisor unavailable: %s", exc)
            return None
        allowed = [name for name in action_names() if name != "custom"]
        allowed.extend(spec.get("name") for spec in extra_actions)
        suggestion = parse_reply(reply if isinstance(reply, str) else str(reply), allowed)
        if suggestion is None:
            logger.info("intent advisor returned nothing usable")
        return suggestion
