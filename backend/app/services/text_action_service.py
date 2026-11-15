"""One action pipeline for typed text, map clicks and the legacy chat surface.

The parser is deterministic and offline: it turns a short player utterance into
the same ActionRequest the structured API accepts. Only when the text cannot be
mapped at all may an optional intent advisor (an LLM) suggest a *candidate*
action, and even then the suggestion is re-validated against the same whitelist
and replayed through the rule engine - the model never writes state.

Bound campaigns are resolved against a world whose scenario_id matches the
campaign's scenario, so a scenario created in the UI keeps working without the
player having to bind a world manually.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.engines.action_registry import action_names, verb_table
from app.engines.event_engine import condition_holds, entity_item_count, entity_location
from app.models.game import ActionRequest, ActionResult, Condition, GameState, WorldDefinition
import logging
import sqlite3

from app.services import intent_advisor, starter_worlds, world_service
from app.services.rejection import rejection_message
from app.services.action_service import ActionService

logger = logging.getLogger(__name__)


def campaign_clock_paused(campaign_id: str) -> bool:
    """Whether the world clock of this campaign is frozen (player left)."""
    try:
        from app.api import routes_game

        return bool(routes_game.campaign_clock_paused(campaign_id))
    except Exception:  # noqa: BLE001 - no runtime means nothing is running
        return True


def _first_sentence(text: str, limit: int = 160) -> str:
    """First non-empty line of an opening narrative, trimmed for a summary."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""

#: The parser accepts exactly the actions the rule catalogue declares, and the
#: verb table is read from the same place, so the two can never drift apart.
ACTIONS = action_names()

#: Verb table. Longest phrases first so "打开" wins over "打".
_VERBS: Sequence[Tuple[str, Tuple[str, ...]]] = verb_table()

#: Spoken durations for `wait`, longest phrase first. The rule receives the
#: number as a param; the parser is the only place that reads prose for it.
_WAIT_DURATIONS: Sequence[Tuple[str, int]] = (
    ("an hour", 60),
    ("one hour", 60),
    ("an hour", 60),
    ("half an hour", 30),
    ("a few minutes", 5),
    ("a minute", 1),
    ("半小时", 30),
    ("半个时辰", 60),
    ("一个时辰", 120),
    ("一小时", 60),
    ("一刻钟", 15),
    ("一会", 10),
    ("一会儿", 10),
    ("片刻", 5),
    ("几分钟", 5),
)

#: The canonical verb per action, used when an advisor has named an action but
#: the utterance itself never contained a word the parser recognises.
_VERBS_PRIMARY: Dict[str, str] = {
    action: verbs[0] for action, verbs in _VERBS if verbs
}
#: Every verb word the catalogue knows, for "did the player start a verb?".
_VERB_WORDS: frozenset = frozenset(
    verb.rstrip() for _action, verbs in _VERBS for verb in verbs if verb.strip()
)

_TRAILING = ("", "。", "！", "?", "？", ".", "吧", "啊", "呢")
#: Leading filler stripped before verb matching, longest phrase first so "我想"
#: wins over "我". `我` alone matters for Chinese utterances like "我拆掉木桌"
#: that would otherwise never reach the verb table. The longer ones (want,
#: plan, try, let me) only mean "this is what I intend", so dropping them
#: leaves the real verb: "我想看看那块木头" -> "看看那块木头" -> look.
_LEADING_PARTICLES = (
    "我想要", "我想", "我要", "我打算", "我准备", "我决定", "我试着", "让我", "帮我",
    "麻烦", "试着", "试图", "请", "我",
)
#: What a place is called in the player's own words. A translated world keeps
#: English ids and names ("Forge"), so "铁匠铺" can only be recognised through
#: the meaning of the place, and this table is that meaning, kept small on
#: purpose: a wrong entry here moves the player somewhere they did not ask for.
_WORD_FOR_HINT: Dict[str, Tuple[str, ...]] = {
    # The Chinese entries are not corroborated by an English name, so a world
    # that keeps them is a world that says "Forge" and means 铁匠铺.
    "forge": ("铁匠", "锻", "打铁", "铁铺", "熔炉", "forg", "smith"),
    "shop": ("商店", "店铺", "杂货", "商人", "柜台", "shop", "store"),
    "tavern": ("酒馆", "客栈", "酒吧", "喝酒", "酒", "inn", "tavern", "bar"),
    "square": ("广场", "集市", "市集", "中心", "market", "square", "plaza"),
    "fields": ("田", "农场", "麦", "野外", "田地", "field", "farm"),
    "warehouse": ("仓库", "货栈", "货仓", "warehouse", "storehouse"),
    "forest": ("森林", "树林", "林子", "forest", "wood"),
    "temple": ("神庙", "神殿", "庙", "temple", "shrine"),
    "gate": ("城门", "大门", "门楼", "gate"),
    "dock": ("码头", "港口", "dock", "harbor", "harbour"),
    "mine": ("矿", "矿井", "矿洞", "mine"),
    "library": ("图书馆", "藏书", "library", "archive"),
}
_ENVELOPE = re.compile(r"^\[([A-Z]+)\]\s*", re.IGNORECASE)
_PARSE_FAILURES = {"missing_target", "ambiguous", "no_target"}


@dataclass
class ParsedAction:
    action: str
    target_id: Optional[str] = None
    matched_name: str = ""
    confidence: str = "exact"
    candidates: List[str] = field(default_factory=list)
    note: str = ""
    #: Extra ActionRequest params the parse produced (e.g. action_id for a
    #: world-authored custom action).
    params: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "target_id": self.target_id,
            "matched_name": self.matched_name,
            "confidence": self.confidence,
            "candidates": list(self.candidates),
            "note": self.note,
            "params": dict(self.params),
        }


def _norm(text: str) -> str:
    value = (text or "").strip()
    envelope = _ENVELOPE.match(value)
    if envelope and envelope[1].upper() in ("DO", "SAY"):
        value = value[envelope.end():].strip()
    for prefix in _LEADING_PARTICLES:
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
    return value


def _verb_prefix(text: str, verb: str) -> bool:
    if not text.startswith(verb):
        return False
    rest = text[len(verb):]
    return not (
        rest and verb[-1].isascii() and verb[-1].isalnum()
        and (rest[0].isalnum() or rest[0] == "_")
    )


#: Verbs that some authors bake into a name ("Open Door", "打开的门").
_VERB_PREFIXES = (
    "open", "unlock", "take", "pick", "use", "talk", "move", "go", "attack",
    "defend", "flee", "buy", "sell", "equip", "unequip", "choose", "look",
    "investigate", "inspect", "examine",
)


def _door_verb(name: str) -> str:
    """'Open Door' -> 'open door'; 'Inner Door' -> '' (no verb baked in)."""
    lowered = " ".join((name or "").lower().split())
    for verb in _VERB_PREFIXES:
        if lowered == verb:
            return ""
        if lowered.startswith(verb + " "):
            return lowered
    return ""


def _clean_target(text: str) -> str:
    value = text.strip().strip("，,、:：")
    for suffix in ("这个", "那个"):
        if value.startswith(suffix):
            value = value[len(suffix):]
    return value.strip()


def _aliases(names: Sequence[str]) -> List[str]:
    """Normalised lookup names, including underscore/spaced spellings.

    Authors write ids as iron_key; players type "iron key". Both spellings
    should resolve to the same candidate, so every name also contributes a
    space-joined and an underscore-joined variant.
    """
    out: List[str] = []
    seen = set()
    for name in names:
        if not name:
            continue
        base = name.strip().lower()
        if not base:
            continue
        for variant in (base, base.replace("_", " "), base.replace(" ", "_")):
            cleaned = " ".join(variant.split())
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                out.append(cleaned)
    return out


class _Candidate:
    __slots__ = ("id", "names", "kind", "verb")

    def __init__(self, item_id: str, names: Sequence[str], kind: str, verb: str = ""):
        self.id = item_id
        self.names = _aliases(names)
        self.kind = kind
        #: Names that describe the action as well as the object ("Open Door").
        #: Ordinary matching would strip the verb from the utterance, so these
        #: names are compared against the whole lowered utterance instead.
        self.verb = verb.strip().lower()

    def match(self, fragment: str) -> int:
        fragment = fragment.strip().lower().replace("_", " ")
        if not fragment:
            return 0
        best = 0
        for name in self.names:
            if name == fragment:
                return 1000 + len(name)
            if fragment.startswith(name) or name.startswith(fragment):
                best = max(best, 500 + min(len(name), len(fragment)))
            elif name in fragment:
                best = max(best, 200 + len(name))
        return best


class TextActionParser:
    """Deterministic utterance -> (action, target) mapping."""

    def parse(
        self,
        text: str,
        world: WorldDefinition,
        state: GameState,
        *,
        improvise: bool = True,
    ) -> Optional[ParsedAction]:
        """Map an utterance to an action.

        `improvise=False` is for the advisor's second look: it must be able to
        tell "I mapped nothing" from "I would record an attempt", otherwise a
        model would never get the chance to name a real action first.
        """
        envelope = _ENVELOPE.match((text or "").strip())
        mode = envelope[1].upper() if envelope else "DO"
        if mode not in ("DO", "SAY"):
            return None
        raw = _norm(text)
        if not raw:
            return None
        lowered = raw.lower()
        for suffix in _TRAILING:
            if suffix and lowered.endswith(suffix):
                lowered = lowered[: -len(suffix)].strip()
        action, fragment = self._split_verb(lowered)
        if mode == "SAY" and action not in (None, "talk"):
            return None
        #: True when a rule did read this sentence and found it impossible
        #: ("开着的那把铁钥匙", "拿起月亮"). Such a sentence must not become an
        #: attempt: "明确动作不许被猜" cuts both ways, and it also keeps the
        #: advisor from re-reading a sentence that already has an answer.
        #: Every `return None` below happens *before* that rule reads anything.
        explained = False
        if action is None:
            if mode == "DO":
                custom = self._split_custom_verb(lowered, world)
                if custom is not None:
                    return self._resolve_custom(custom[0], custom[1], world, state)
            guessed = self._guess_without_verb(lowered, world, state)
            if guessed is not None and (mode == "DO" or guessed.action == "talk"):
                # A guess is only a hint: if the words did not really name the
                # thing, the attempt is still an attempt.
                if guessed.confidence not in _PARSE_FAILURES:
                    return guessed
            if mode == "SAY":
                # "老板，这块铁怎么卖？" is speech. The rule for talking needs
                # somebody here, and it says who is - the same answer the
                # advisor would give, without paying for a model call.
                return self._addressable(state, world, "talk", raw)
            if mode == "DO" and improvise and not explained and self._improvise_allowed(lowered):
                return self._improvise(lowered, world, state)
            return None
        parsed = self._resolve(action, fragment, world, state, utterance=text)
        if parsed is not None:
            return parsed
        if action in ("move", "travel") and fragment:
            # "去铁匠铺" is how people actually name a place: they shorten it.
            # This runs after the exact resolver so an ordinary name always
            # wins; it only stops a shortened place name from becoming
            # missing_target. Stop at the place name: "去铁匠铺看看" is a move,
            # not a look - the suffix names the errand, not the verb.
            loose = self._loose_place(fragment, world, state)
            if loose is not None:
                return loose
        # The rule behind this verb ran and refused the sentence. This is a
        # complete answer, not an unmapped one.
        explained = True
        return ParsedAction(
            action=action,
            confidence="missing_target",
            note=f"could not resolve a target from '{fragment}'",
        )

    @staticmethod
    def _addressable(
        state: GameState, world: WorldDefinition, action: str, text: str
    ) -> Optional[ParsedAction]:
        """The only sensible target of `talk` when nobody was named.

        A player who types speech expects the person in front of them to hear
        it; the rule then answers with who is actually here, which is exactly
        the hint a rejection would have given.
        """
        from app.engines.event_engine import entity_location

        here = [
            entity
            for entity in world.entities
            if entity_location(state, world, entity.id) == state.location_id
        ]
        if not here:
            return ParsedAction(
                action="talk",
                confidence="missing_target",
                note="nobody is here to hear it",
            )
        return ParsedAction(
            action=action,
            confidence="spoken",
            note="said it out loud here",
        )

    @staticmethod
    def _improvise_allowed(lowered: str) -> bool:
        """Whether an unmapped sentence is worth recording as an attempt.

        Two utterances are deliberately *not* improvised:

        * one that names a thing here and nothing else ("iron") - the player
          already named the object, they just have not said what to do with it;
        * one that spells a real verb with a prefix ("takeover iron_key",
          "lookout guard") - that is a slip, and recording "takeover" as an
          attempt teaches the player nothing while it looks like it worked.
        """
        text = (lowered or "").strip()
        # One clause is enough: Chinese has no spaces, so the word count says
        # nothing about whether a sentence was written. A single Latin word is
        # not one - it is a bare name, and naming a thing is not an attempt.
        if not text:
            return False
        if text.isascii() and len(text.split()) < 2:
            return False
        if not text.isascii() and len(text) < 2:
            return False
        words = text.split()
        first = words[0].strip(",.;:!?，。；：") if words else ""
        if (
            len(first) > 2
            and first not in _VERB_WORDS
            and any(
                # English verbs are space-delimited, so the "word" has to be
                # understood without spaces: "openly", "guardrail", "takeover".
                first.startswith(verb) and first[len(verb):].isalpha()
                for verb in _VERB_WORDS
                if verb.isascii()
            )
        ):
            # A real verb with something stuck to it: the writer meant a verb,
            # just not as one. Recording that as a free attempt would swallow
            # the typo silently, so it stays a parse error.
            return False
        for _action, verbs in _VERBS:
            for verb in verbs:
                verb = verb.rstrip()
                if verb and any(
                    text.startswith(verb + extra) for extra in ("over", "out", "ing")
                ):
                    return False
        return True

    def _improvise(
        self, lowered: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        """Anything else the player described: attempt it, do not reject it.

        Free text is the point of a text adventure. The rule engine still owns
        the outcome - `_improvise` records the attempt and changes nothing
        else - so this is not a bypass, it is a verb the world always has.
        """
        text = (lowered or "").strip()
        if not text:
            return None
        # The envelope ([DO], [SAY]) is our own syntax: it is stripped here so
        # the recorded attempt reads like what the player wrote.
        text = _ENVELOPE.sub("", text).strip()
        text = text.strip("[]").strip()
        return ParsedAction(
            action="improvise",
            confidence="improvised",
            note="not a defined verb: attempted as written",
            params={"attempt": text[:280]},
        )

    def _loose_place(
        self, fragment: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        """Resolve a shortened place name, but only when exactly one place fits.

        "铁匠铺" against "Forge" is not a name match; it is a meaning match. The
        cheap approximation is a shared character run with the place's name or
        the words in that place's own description, and only a unique hit is
        accepted - an ambiguous one stays a rejection, because guessing between
        two places would move the player somewhere they never asked for.
        """
        names = {fragment}
        for prefix in ("去", "到", "前往", "走"):
            if fragment.startswith(prefix):
                names.add(fragment[len(prefix):].strip())
        # A place's own name is the best evidence there is: if one place is
        # named anywhere in the utterance, the errand after it ("看看", "一趟")
        # changes nothing.
        for location in world.locations:
            for alias in (location.name, location.id):
                alias = (alias or "").strip().lower()
                if alias and alias in fragment:
                    return ParsedAction(
                        action="move",
                        target_id=location.id,
                        matched_name=location.name,
                        confidence="named",
                        note=f"named {location.name} inside '{fragment}'",
                    )
        for enclitic in ("那里", "那儿", "去看看", "看看", "看看去", "一趟"):
            for name in list(names):
                if name.endswith(enclitic):
                    names.add(name[: -len(enclitic)].strip())
        scored: List[Tuple[int, str]] = []
        for location in world.locations:
            haystack = f"{location.id} {location.name}".lower()
            for name in names:
                if not name:
                    continue
                if name in haystack:
                    scored.append((1000 + len(name), location.id))
                    break
                # The player's own word for a place ("铁匠铺") is not in its
                # English name, so the Chinese description an author wrote is
                # part of the evidence. Without it a translated world would
                # reject every shortened name.
                hint = (
                    f"{getattr(location, 'description', '') or ''}"
                    f" {getattr(location, 'name', '')}"
                ).lower()
                # The player's word has to be inside the utterance, and it has
                # to actually mean this place: a table entry that no world word
                # corroborates is not evidence.
                corroborated = [
                    word
                    for word in _WORD_FOR_HINT.get(location.id, ())
                    if not word.isascii() or word in hint or word in location.id.lower()
                ]
                if any(word in name for word in corroborated):
                    scored.append((40, location.id))
                    break
        if not scored:
            return None
        scored.sort(reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        location_id = scored[0][1]
        target = next((l for l in world.locations if l.id == location_id), None)
        return ParsedAction(
            action="move",
            target_id=location_id,
            matched_name=getattr(target, "name", location_id),
            confidence="loose",
            note=f"matched a place from '{fragment}'",
        )

    def _split_verb(self, text: str) -> Tuple[Optional[str], str]:
        for action, verbs in _VERBS:
            for verb in verbs:
                verb = verb.rstrip()
                if _verb_prefix(text, verb):
                    return action, _clean_target(text[len(verb):])
        return None, text

    # -- world-authored actions --------------------------------------------
    @staticmethod
    def _custom_verbs(world: WorldDefinition) -> List[Tuple[str, str]]:
        """(verb, custom action id) pairs, longest phrase first."""
        pairs: List[Tuple[str, str]] = []
        for spec in getattr(world, "custom_actions", []) or []:
            for verb in spec.verbs:
                cleaned = (verb or "").strip().lower()
                if cleaned:
                    pairs.append((cleaned, spec.id))
        pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        return pairs

    def _split_custom_verb(
        self, text: str, world: WorldDefinition
    ) -> Optional[Tuple[str, str]]:
        for verb, action_id in self._custom_verbs(world):
            if _verb_prefix(text, verb):
                return action_id, _clean_target(text[len(verb):])
        return None

    def _resolve_custom(
        self, action_id: str, fragment: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        spec = next(
            (
                item
                for item in getattr(world, "custom_actions", []) or []
                if item.id == action_id
            ),
            None,
        )
        if spec is None:
            return None
        label = spec.label or spec.id
        params = {"action_id": spec.id}
        if spec.target == "none" or not fragment:
            if spec.target == "required":
                return ParsedAction(
                    action="custom",
                    confidence="missing_target",
                    note=f"{label} needs a target.",
                    params=params,
                )
            return ParsedAction(action="custom", confidence="verb_only", params=params)
        matched = self._best(fragment, self._custom_candidates(world, state))
        if matched is None:
            if spec.target == "required":
                return ParsedAction(
                    action="custom",
                    confidence="missing_target",
                    note=f"could not resolve a target from '{fragment}'",
                    params=params,
                )
            return ParsedAction(action="custom", confidence="verb_only", params=params)
        return ParsedAction(
            action="custom",
            target_id=matched.id,
            matched_name=matched.names[0] if matched.names else matched.id,
            confidence="exact",
            params=params,
        )

    def _custom_candidates(
        self, world: WorldDefinition, state: GameState
    ) -> List[_Candidate]:
        """Anything the player could name as the target of a world action."""
        candidates: List[_Candidate] = []
        seen = set()
        for candidate in (
            self._move_candidates(world, state)
            + self._item_candidates(world, state)
            + self._door_candidates(world, state)
            + self._entity_candidates(world, state)
            + self._talk_candidates(world, state)
        ):
            if candidate.id in seen:
                continue
            seen.add(candidate.id)
            candidates.append(candidate)
        return candidates

    @staticmethod
    def _free_form_look(text: str) -> Optional[ParsedAction]:
        """把一句规则表外的自然语言降级成「看清这地方」。

        只对 DO（或无信封的自由输入）生效：SAY/META/CONTINUE 是玩家在聊天或
        跳过，硬掰成动作反而是替玩家做主。理由写在 execute() 调用处。
        """
        raw = (text or "").strip()
        envelope = _ENVELOPE.match(raw)
        mode = envelope[1].upper() if envelope else "DO"
        if mode != "DO":
            return None
        body = _norm(raw)
        if len(body) < 2:
            return None
        return ParsedAction(
            action="look",
            confidence="verb_only",
            note="",
            params={"free_form": body[:400]},
        )

    def _guess_without_verb(
        self, text: str, world: WorldDefinition, state: GameState
    ) -> Optional[ParsedAction]:
        candidates: List[Tuple[str, _Candidate]] = []
        candidates.extend(("move", c) for c in self._move_candidates(world, state))
        candidates.extend(("take", c) for c in self._item_candidates(world, state))
        candidates.extend(("talk", c) for c in self._talk_candidates(world, state))
        candidates.extend(("open", c) for c in self._door_candidates(world, state))
        matches = {
            (action, candidate.id): candidate
            for action, candidate in candidates
            if candidate.match(text) >= 1000
        }
        if len(matches) != 1:
            return None
        (action, _target_id), candidate = next(iter(matches.items()))
        return ParsedAction(
            action=action,
            target_id=candidate.id,
            matched_name=self._display(candidate, world, action),
            confidence="name_only",
        )

    def _resolve(
        self,
        action: str,
        fragment: str,
        world: WorldDefinition,
        state: GameState,
        utterance: str = "",
    ) -> Optional[ParsedAction]:
        if action == "look":
            if fragment in ("", "around", "周围", "四周"):
                return ParsedAction(action="look", confidence="verb_only")
            return ParsedAction(action="look", target_id=fragment)
        if action == "flee":
            return ParsedAction(action="flee", confidence="verb_only")
        if action in ("attack", "defend"):
            enemies = self._enemy_candidates(world, state)
            matched = None
            if fragment:
                candidates = self._entity_candidates(world, state)
                matched = self._best(fragment, candidates, exact_only=True)
                if matched is None and fragment.startswith("the "):
                    matched = self._best(fragment[4:], candidates, exact_only=True)
                if matched is None:
                    return ParsedAction(
                        action=action,
                        confidence="missing_target",
                        note=f"could not resolve an unambiguous target from '{fragment}'",
                    )
            elif action == "attack":
                matched = enemies[0] if len(enemies) == 1 else None
                if matched is None:
                    return ParsedAction(
                        action=action,
                        confidence="ambiguous" if enemies else "no_target",
                        note="Name one living enemy here to attack.",
                    )
            return ParsedAction(
                action=action,
                target_id=matched.id if matched else None,
                matched_name=self._display(matched, world, action) if matched else "",
                confidence="exact" if matched else "verb_only",
            )
        if action in ("buy", "sell"):
            candidates = (
                self._shop_candidates(world, state)
                if action == "buy"
                else self._item_candidates(world, state)
            )
            matched = self._best(fragment, candidates) if fragment else None
            if matched is None:
                return ParsedAction(
                    action=action,
                    confidence="missing_target",
                    note=f"could not resolve a target from '{fragment}'",
                )
            return ParsedAction(
                action=action,
                target_id=matched.id,
                matched_name=self._display(matched, world, action),
                confidence="exact",
            )
        if action == "investigate":
            if not fragment:
                return ParsedAction(action="investigate", confidence="verb_only")
            candidates = (
                self._entity_candidates(world, state)
                + self._item_candidates(world, state)
                + self._door_candidates(world, state)
                + self._move_candidates(world, state)
            )
            matched = self._best(fragment, candidates)
            if matched is None:
                return ParsedAction(
                    action="investigate",
                    confidence="missing_target",
                    note=f"could not resolve a target from '{fragment}'",
                )
            return ParsedAction(
                action="investigate",
                target_id=matched.id,
                matched_name=self._display(matched, world, "investigate"),
                confidence="exact",
            )
        if action == "talk" and not fragment:
            return ParsedAction(action="talk", confidence="verb_only")
        if action == "wait":
            # Waiting has no target: the duration lives in params so the rule
            # never has to parse prose.
            params = {}
            for word, value in _WAIT_DURATIONS:
                if word in fragment or word in utterance:
                    params["minutes"] = value
                    break
            return ParsedAction(action="wait", confidence="verb_only", params=params)
        if action in ("dismantle", "combine"):
            if not fragment:
                return ParsedAction(
                    action=action,
                    confidence="missing_target",
                    note=f"{action} needs a target.",
                )
            # dismantle acts on what you hold; combine names what you want to
            # build, which by definition you do not hold yet. Both stay inside
            # the rule engine's authority: the parser only resolves an id.
            candidates = self._carried_candidates(world, state)
            if action == "combine":
                held = {candidate.id for candidate in candidates}
                candidates = candidates + [
                    _Candidate(item.id, [item.name, item.id], "item")
                    for item in world.items
                    if item.craftable and item.id not in held
                ]
            matched = self._best(
                fragment, candidates, whole=utterance or fragment
            )
            if matched is None:
                return None
            return ParsedAction(
                action=action,
                target_id=matched.id,
                matched_name=self._display(matched, world, action),
                confidence="exact",
            )
        if action == "move":
            rules = ("move", self._move_candidates(world, state))
        elif action in ("take", "use", "equip"):
            rules = (action, self._item_candidates(world, state))
        elif action == "unequip":
            if not fragment:
                return ParsedAction(action="unequip", confidence="verb_only")
            rules = ("unequip", self._item_candidates(world, state))
        elif action == "open":
            rules = ("open", self._door_candidates(world, state))
        elif action == "talk":
            rules = ("talk", self._talk_candidates(world, state))
        else:
            return None
        matched = self._best(fragment, rules[1], whole=utterance or fragment)
        if matched is None:
            return None
        return ParsedAction(
            action=rules[0],
            target_id=matched.id,
            matched_name=self._display(matched, world, rules[0]),
            confidence="exact",
        )

    @staticmethod
    def _best(
        fragment: str,
        candidates: List[_Candidate],
        exact_only: bool = False,
        whole: str = "",
    ) -> Optional[_Candidate]:
        def score(candidate: "_Candidate") -> int:
            best = candidate.match(fragment)
            if whole and candidate.verb:
                # The authored name already contains the verb, so the verb was
                # stripped from the utterance before matching; compare against
                # the full sentence instead.
                if _norm(whole) == _norm(candidate.verb) or _norm(whole).startswith(candidate.verb + " "):
                    if not fragment or fragment in candidate.verb or candidate.verb.endswith(fragment):
                        best = max(best, 900)
            return best

        ranked = sorted(
            ((score(candidate), candidate) for candidate in candidates),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] == 0:
            return None
        if exact_only and ranked[0][0] < 1000:
            return None
        if len(ranked) > 1 and ranked[1][0] == ranked[0][0] and ranked[0][0] > 0:
            return None  # ambiguous, let the caller report candidates
        return ranked[0][1]

    @staticmethod
    def _display(candidate: _Candidate, world: WorldDefinition, action: str) -> str:
        if action in ("buy", "sell"):
            candidates = (
                self._shop_candidates(world, state)
                if action == "buy"
                else self._item_candidates(world, state)
            )
            matched = self._best(fragment, candidates) if fragment else None
            if matched is None:
                return ParsedAction(
                    action=action,
                    confidence="missing_target",
                    note=f"could not resolve a target from '{fragment}'",
                )
            return ParsedAction(
                action=action,
                target_id=matched.id,
                matched_name=self._display(matched, world, action),
                confidence="exact",
            )
        if action == "investigate":
            for collection in (world.entities, world.items, world.doors, world.locations):
                for entry in collection:
                    if entry.id == candidate.id:
                        return entry.name
        if action == "move":
            for location in world.locations:
                if location.id == candidate.id:
                    return location.name
        if action in ("take", "use", "equip", "unequip", "buy", "sell"):
            for item in world.items:
                if item.id == candidate.id:
                    return item.name
        if action == "open":
            for door in world.doors:
                if door.id == candidate.id:
                    return door.name
        if action in ("talk", "attack", "defend"):
            for entity in world.entities:
                if entity.id == candidate.id:
                    return entity.name
        return candidate.id

    @staticmethod
    def _move_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        locations = {location.id: location for location in world.locations}
        here = locations.get(state.location_id)
        if here is None:
            return []
        candidates = []
        for exit_def in here.exits:
            target = locations.get(exit_def.target_id)
            if target is not None:
                candidates.append(_Candidate(target.id, [target.name, target.id, exit_def.label], "location"))
        return candidates

    @staticmethod
    def _item_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        items = []
        for item in world.items:
            if state.inventory.get(item.id, 0) > 0:
                items.append(_Candidate(item.id, [item.name, item.id], "item"))
            elif state.item_locations.get(item.id) == state.location_id:
                items.append(_Candidate(item.id, [item.name, item.id], "item"))
            elif any(
                entity_location(state, world, entity.id) == state.location_id
                and entity_item_count(state, world, entity.id, item.id) > 0
                for entity in world.entities
            ):
                items.append(_Candidate(item.id, [item.name, item.id], "item"))
        return items

    @staticmethod
    def _carried_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        """Only what the player is holding: dismantle/combine act on inventory.

        The `take` table also lists items lying on the floor; offering them here
        would make "拆掉木桌" resolve to a table the player is not holding and
        then fail in the rule instead of in the parser.
        """
        return [
            _Candidate(item.id, [item.name, item.id], "item")
            for item in world.items
            if state.inventory.get(item.id, 0) > 0
        ]

    @staticmethod
    def _shop_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        items = {item.id: item for item in world.items}
        seen = set()
        candidates = []
        for entity in world.entities:
            if entity_location(state, world, entity.id) != state.location_id:
                continue
            for item_id in entity.shop_items:
                if item_id in seen or item_id not in items:
                    continue
                seen.add(item_id)
                candidates.append(
                    _Candidate(item_id, [items[item_id].name, item_id], "item")
                )
        return candidates

    @staticmethod
    def _door_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        return [
            _Candidate(door.id, [door.name, door.id], "door", verb=_door_verb(door.name))
            for door in world.doors
            if door.location_id == state.location_id
        ]

    @staticmethod
    def _entity_candidates(world: WorldDefinition, state: GameState) -> List[_Candidate]:
        return [
            _Candidate(entity.id, [entity.name, entity.id], entity.kind)
            for entity in world.entities
            if entity_location(state, world, entity.id) == state.location_id
            and condition_holds(state, world, Condition(kind="entity_alive", target=entity.id))
        ]

    @classmethod
    def _enemy_candidates(cls, world: WorldDefinition, state: GameState) -> List[_Candidate]:
        eligible = {
            entity.id for entity in world.entities
            if entity.kind != "object"
            and (entity.kind == "creature" or entity.hostile
                 or entity.id == state.combat.get("target_id"))
        }
        return [c for c in cls._entity_candidates(world, state) if c.id in eligible]

    @classmethod
    def _talk_candidates(cls, world: WorldDefinition, state: GameState) -> List[_Candidate]:
        candidates = [
            candidate for candidate in cls._entity_candidates(world, state)
            if candidate.kind != "object"
        ]
        known = {entity.id for entity in world.entities}
        location = next((loc for loc in world.locations if loc.id == state.location_id), None)
        for npc_id in getattr(location, "npc_ids", []) if location else []:
            if npc_id not in known:
                candidates.append(_Candidate(npc_id, [npc_id], "entity"))
        return candidates


class TextActionService:
    """Turn one utterance into exactly one committed, idempotent action."""

    def __init__(
        self,
        game_store,
        scenario_store=None,
        action_service: Optional[ActionService] = None,
        parser: Optional[TextActionParser] = None,
        advisor: Optional[intent_advisor.IntentAdvisor] = None,
    ):
        self.game_store = game_store
        self.scenario_store = scenario_store
        self.action_service = action_service or ActionService(
            game_store,
            clock_paused=lambda campaign_id: campaign_clock_paused(campaign_id),
        )
        self.parser = parser or TextActionParser()
        #: Optional LLM that may *suggest* a mapping when the parser finds none.
        #: Off unless ENGINE_INTENT_ADVISOR is set; even when on, its suggestion
        #: is re-parsed and re-validated, and the rule engine still decides.
        self.advisor = advisor

    # ---------------------------------------------------------------- binding
    def bindable_world(self, campaign_id: str) -> Optional[WorldDefinition]:
        """Return the world for a campaign, binding (or creating) one if needed.

        Resolution order, all deterministic:
        1. an already bound campaign keeps its world (never rebound here);
        2. a world authored for the campaign's scenario, in creation order;
        3. a starter world generated from the scenario itself.

        Step 3 is what keeps a brand new campaign inside the rules lane: without
        it the caller fell back to the legacy LLM session, whose state this
        store cannot see, so the two systems disagreed about the same player.
        """
        if self.game_store.get_state(campaign_id) is not None:
            return None
        if self.scenario_store is None:
            return None
        campaign = self.scenario_store.get_campaign(campaign_id)
        if campaign is None:
            return None
        for summary in self.game_store.list_worlds():
            if summary.get("scenario_id") != campaign.scenario_id:
                continue
            world = self.game_store.get_world(summary["world_id"])
            if world is not None:
                world_service.bind_world(self.game_store, campaign_id, world.world_id)
                return world
        return self._provision_starter_world(campaign_id, campaign)

    def _scenario_record(self, scenario_id: str):
        if self.scenario_store is None or not hasattr(self.scenario_store, "get_scenario"):
            return None
        try:
            return self.scenario_store.get_scenario(scenario_id)
        except Exception:  # noqa: BLE001 - a missing scenario falls back to defaults
            logger.warning("could not read scenario %s", scenario_id, exc_info=True)
            return None

    def _provision_starter_world(self, campaign_id: str, campaign) -> Optional[WorldDefinition]:
        """Create and bind a starter world for a campaign that has none.

        The document is generated as plain data and validated with the same
        `validate_world` any authored world goes through; an invalid document is
        never saved. A world id that already exists is skipped (never
        overwritten), and a campaign that already has state is left alone.
        """
        scenario = self._scenario_record(getattr(campaign, "scenario_id", ""))
        language = getattr(scenario, "language", "") or ""
        title = getattr(scenario, "title", "") or ""
        summary = (
            getattr(scenario, "description", "")
            or _first_sentence(getattr(scenario, "opening_narrative", ""))
            or ""
        )
        world_type = getattr(scenario, "world_type", "") or ""
        base_id = starter_worlds.starter_world_id(campaign_id)
        for attempt in range(0, 6):
            world_id = starter_worlds.alternate_world_id(base_id, attempt)
            if self.game_store.get_world(world_id) is not None:
                continue
            document = starter_worlds.build_world_document(
                scenario_id=getattr(campaign, "scenario_id", ""),
                world_id=world_id,
                title=title,
                summary=summary,
                language=language,
                world_type=world_type,
            )
            try:
                world = WorldDefinition(**document)
            except Exception:  # noqa: BLE001 - never bind an invalid generated world
                logger.error(
                    "generated starter world for campaign %s failed model validation",
                    campaign_id, exc_info=True,
                )
                return None
            errors = world_service.validate_world(world)
            if errors:
                logger.error(
                    "generated starter world for campaign %s is invalid: %s",
                    campaign_id, "; ".join(errors),
                )
                return None
            world_service.save_world(self.game_store, world)
            state = world_service.bind_world(self.game_store, campaign_id, world.world_id)
            logger.info(
                "campaign %s auto-bound generated starter world %s (%s)",
                campaign_id, world.world_id, world.name,
            )
            del state
            return world
        logger.error("no free starter world id for campaign %s", campaign_id)
        return None

    def _world_for_state(self, state: GameState, campaign_id: str) -> WorldDefinition:
        """The world a bound state points at; a starter world if it went missing.

        A state can outlive its world document (a world deleted while a campaign
        still points at it). Rather than failing the turn, the campaign keeps its
        progress and gets a freshly generated starter world of the same shape.
        """
        try:
            return world_service.get_world(self.game_store, state.world_id)
        except LookupError:
            logger.error(
                "campaign %s points at missing world %s; provisioning a starter",
                campaign_id, state.world_id,
            )
        campaign = (
            self.scenario_store.get_campaign(campaign_id)
            if self.scenario_store is not None else None
        )
        world = self._provision_starter_world(campaign_id, campaign) if campaign else None
        if world is None:
            raise LookupError(f"campaign '{campaign_id}' has no usable world")
        return world

    def ensure_state(self, campaign_id: str) -> GameState:
        state = self.game_store.get_state(campaign_id)
        if state is not None:
            return state
        self.bindable_world(campaign_id)
        state = self.game_store.get_state(campaign_id)
        if state is None:
            raise LookupError(
                f"campaign '{campaign_id}' has no bound world; bind one first"
            )
        return state

    # --------------------------------------------------------------- execute
    async def execute(
        self,
        campaign_id: str,
        text: str,
        request_id: Optional[str] = None,
        narrate: bool = True,
    ) -> Dict[str, Any]:
        request_id = request_id or f"txt-{uuid.uuid4().hex}"
        replayed = self.game_store.get_request(request_id)
        if replayed is not None:
            return await self._replay_text(campaign_id, text, request_id, narrate, replayed)
        state = self.ensure_state(campaign_id)
        world = self._world_for_state(state, campaign_id)
        # 1) 确定性解析。2) 认不出来的句子先让可选顾问看一眼（它只能给一个
        # 候选动作 + 目标片段，仍然要过同一套解析与规则）。3) 顾问也给不出
        # 任何动作时，这句话仍然是一次「尝试」：规则记录尝试、花掉时间，但
        # 不改变任何状态。这样自由输入不会被当成错句，动作也不会被猜。
        parsed = self.parser.parse(text, world, state, improvise=False)
        advised = None
        # A rule is the cheapest and most honest reader. Only a sentence that no
        # rule could map ("蹲下来看看裂缝", "把门推开") is worth a model call: a
        # refusal from a real rule ("你拿不动月亮") is already an answer, and an
        # attempt (conf. improvised, only reachable with improvise=True) is what
        # happens when the model found nothing either.
        unmapped = parsed is None or not parsed.action
        if unmapped:
            advised = await self._advise(text, world, state, campaign_id)
            if advised is not None:
                parsed = advised
            else:
                parsed = self.parser.parse(text, world, state)
        # 明确写了动词、只是目标不存在：仍然拒绝，并把现场能做什么写进
        # message（见 _reject_parse）。「明确动作不许被猜」这条不变量没变。
        if parsed is None or not parsed.action or parsed.confidence in _PARSE_FAILURES:
            return await self._reject_parse(campaign_id, request_id, text, state, parsed)
        request = ActionRequest(
            campaign_id=campaign_id,
            request_id=request_id,
            action=parsed.action,
            target_id=parsed.target_id,
            params={**parsed.params, "text": text},
            narrate=narrate,
        )
        result = await self.action_service.execute(request)
        return {
            "request_id": request_id,
            "status": result.status,
            "parsed": parsed.as_dict(),
            "result": json.loads(result.model_dump_json()),
        }

    async def _replay_text(
        self, campaign_id: str, text: str, request_id: str, narrate: bool, row: Dict[str, Any]
    ) -> Dict[str, Any]:
        payload = row["request"]
        current = self.game_store.get_state(campaign_id)
        if (
            row["campaign_id"] != campaign_id
            or payload.get("campaign_id") != campaign_id
            or payload.get("actor_id") != "player"
            or payload.get("actor_type", "player") != "player"
            or payload.get("params", {}).get("text") != text
        ):
            result = ActionResult(
                request_id=request_id,
                campaign_id=campaign_id,
                status="conflict",
                action="unknown",
                code="request_id_conflict",
                message="request_id was already used for a different campaign or payload",
                state_version=current.version if current is not None else 0,
                state=current,
            )
        elif payload.get("action") == "unknown":
            result = self.action_service._load_result(row)
            result.status = "duplicate"
            result.message = "duplicate request ignored; no side effects were applied"
            result.state_version = current.version if current is not None else row["state_version"]
            result.state = current
        else:
            request = ActionRequest.model_validate({**payload, "narrate": narrate})
            result = await self.action_service.execute(request)
        return {
            "request_id": request_id,
            "status": result.status,
            "parsed": None,
            "result": json.loads(result.model_dump_json()),
        }

    def _intent_router(self, campaign_id: str):
        """Router from the scenario's intent preset, or None to keep the global one."""
        if self.scenario_store is None:
            return None
        try:
            campaign = self.scenario_store.get_campaign(campaign_id)
            if campaign is None:
                return None
            config = self.scenario_store.get_play_config(campaign.scenario_id)
            preset = (
                self.scenario_store.get_model_preset(config.intent_preset_id)
                if config.intent_preset_id
                else None
            )
            if preset is None or preset.role not in ("intent", "any") or not preset.provider:
                return None
            from app.services.narrative_service import preset_router

            return preset_router(
                {"provider": preset.provider, "model": preset.model}
            )
        except Exception:  # noqa: BLE001 - advice is optional
            return None

    async def _advise(
        self, text: str, world: WorldDefinition, state: GameState, campaign_id: str
    ) -> Optional[ParsedAction]:
        """Second chance for an utterance the deterministic parser did not map.

        The suggestion is fed back through the same parser, so the model can
        only ever steer *which* verb table entry is tried - the resolution of
        the target, the preconditions and the effects stay in code.
        """
        envelope = _ENVELOPE.match((text or "").strip())
        mode = envelope[1].upper() if envelope else "DO"
        if mode not in ("DO", "SAY") or not intent_advisor.enabled():
            return None
        advisor = self.advisor
        router = self._intent_router(campaign_id) if advisor is not None else None
        if router is not None:
            advisor = intent_advisor.IntentAdvisor(router)
        if advisor is None:
            logger.info("intent advisor is enabled but not wired; keeping the rejection")
            return None
        extra = [
            {"name": action.id, "description": action.label or action.description}
            for action in getattr(world, "custom_actions", []) or []
        ]
        try:
            suggestion = await advisor.suggest(text, world, extra)
        except Exception as exc:  # noqa: BLE001 - advice must never break a turn
            logger.info("intent advisor failed: %s", exc)
            return None
        if suggestion is None or (mode == "SAY" and suggestion.action != "talk"):
            return None
        # The model names an action and the words it thinks name the target.
        # Rebuild a verb+target utterance from exactly those two pieces: the
        # parser then resolves the target against the real world, so an
        # invented target cannot survive and the verb table stays in charge.
        primary = _VERBS_PRIMARY.get(suggestion.action, "")
        candidate = f"{primary} {suggestion.fragment}".strip() if primary else ""
        if not candidate:
            return None
        parsed = self.parser.parse(candidate, world, state)
        if parsed is None or not parsed.action:
            return None
        if parsed.action != suggestion.action and suggestion.action != "custom":
            # The parser disagrees with the model about which verb this is. The
            # parser wins: it matched real words against the real verb table.
            logger.info(
                "intent advisor suggested %s, parser says %s; keeping the parser",
                suggestion.action,
                parsed.action,
            )
        parsed.note = (parsed.note + " " if parsed.note else "") + "intent advisor"
        if parsed.confidence not in _PARSE_FAILURES:
            parsed.confidence = "advised"
        return parsed

    async def _reject_parse(
        self, campaign_id: str, request_id: str, text: str, state: GameState,
        parsed: Optional[ParsedAction] = None,
    ) -> Dict[str, Any]:
        from app.models.game import NarrativeResult

        # 没有叙事不等于没有反馈：玩家要么知道下一步能做什么，要么就会觉得
        # 「对话无响应」。提示必须用这个世界真实的出口/人物/物品来说话。
        world = self._world_for_state(state, campaign_id)
        message = rejection_message(
            world, state,
            text=text,
            note=parsed.note if parsed else "",
            code=(parsed.confidence if parsed else "unparsed_action"),
        )
        result = ActionResult(
            request_id=request_id,
            campaign_id=campaign_id,
            status="rejected",
            action=parsed.action if parsed else "unknown",
            actor_type="player",
            ok=False,
            code=parsed.confidence if parsed else "unparsed_action",
            message=message,
            state_version=state.version,
            state=state,
            narrative=NarrativeResult(status="skipped", error=message),
        )
        request_payload = {
            "campaign_id": campaign_id,
            "request_id": request_id,
            "action": result.action,
            "actor_id": "player",
            "actor_type": "player",
            "params": {**(parsed.params if parsed else {}), "text": text},
        }
        payload = json.loads(result.model_dump_json())
        try:
            self.game_store.record_request(
                campaign_id=campaign_id,
                request_id=request_id,
                request_payload=request_payload,
                status="rejected",
                result_payload=payload,
                state_version=state.version,
            )
        except sqlite3.IntegrityError:
            stored = self.game_store.get_request(request_id)
            if stored is None:
                raise
            return await self._replay_text(campaign_id, text, request_id, False, stored)
        return {
            "request_id": request_id,
            "status": "rejected",
            "parsed": parsed.as_dict() if parsed else None,
            "result": payload,
        }
