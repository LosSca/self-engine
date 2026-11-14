"""What one NPC wants to do right now, decided by utility.

Phase 3 of docs/hybrid-world-simulation.md, and deliberately Level 2: a fixed
list of *candidate intents*, each scored by a handful of readable numbers, the
best one winning. No goal lattice, no planner, no search - a plan here is a
short primitive sequence, and every step of it is still an ActionRequest that
the rules engine may refuse.

Two separations matter more than the scoring itself:

* **personality and needs live on the NPC, motive lives in this module.** The
  same person with the same hunger chooses differently because the *world* is
  different - the ore is gone, the forge is cold, a guard saw you at the door.
* **the NPC only knows what it perceived.** The caller passes `known` (facts the
  NPC witnessed or was told), never the world's full truth, so "the system knows
  it" can never become "the NPC knows it".

Determinism: every tie is broken by the intent name, so the same state always
produces the same choice, which is what makes a run replayable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Personality trait every NPC is scored with, all defaulting to 0.5 (neutral).
TRAITS: Tuple[str, ...] = ("diligence", "sociability", "caution", "boldness", "greed")

#: Trait multipliers per intent: (trait, weight). An intent with no trait still
#: gets `base`; a trait of 0.5 is neutral, so authored NPCs without any traits
#: behave exactly like each other and the utility comes from the world alone.
INTENTS: Dict[str, Dict[str, Any]] = {
    "work": {
        "base": 0.70,
        "traits": (("diligence", 0.30),),
        "needs": (("energy", 0.30),),
    },
    "eat": {
        "base": 0.10,
        "traits": (),
        # Hunger is a *survival* motive: at zero it outranks a diligent smith's
        # work. That ordering is the point - a utility model where routine always
        # wins is just a timetable with extra arithmetic. (Ungated, this is the
        # baseline "I am a bit peckish" score, which must stay below work.)
        "needs": (("hunger", 1.10),),
    },
    "rest": {
        "base": 0.10,
        "traits": (("diligence", -0.20),),
        "needs": (("energy", 0.85),),
    },
    "socialise": {
        "base": 0.15,
        "traits": (("sociability", 0.35),),
        "needs": (("social", 0.40),),
    },
    "patrol": {
        "base": 0.15,
        "traits": (("caution", 0.30), ("boldness", 0.10)),
        "needs": (),
    },
    "investigate": {
        "base": 0.05,
        "traits": (("caution", 0.25),),
        "needs": (),
    },
}

#: Needs every NPC tracks in [0, 1]; 1 means "fully satisfied".
DEFAULT_NEEDS: Dict[str, float] = {
    "hunger": 0.8,
    "energy": 0.8,
    "social": 0.6,
}

#: Below this adjusted score the NPC keeps doing what it is already doing. It is
#: hysteresis, not laziness: without it a smith with two equally plausible
#: options would walk between the forge and the house every single tick.
STICKINESS = 0.08


@dataclass
class Intent:
    """A scored candidate, kept with its reasons for the debug panel."""

    name: str
    score: float
    terms: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "score": round(self.score, 4), "terms": self.terms}


def _trait(traits: Optional[Dict[str, Any]], name: str) -> float:
    raw = (traits or {}).get(name, 0.5)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, value))


def _deficit(needs: Dict[str, float], name: str) -> float:
    """How unmet a need is, in [0, 1]: 1 = completely unsatisfied."""
    try:
        value = float((needs or {}).get(name, DEFAULT_NEEDS.get(name, 0.5)))
    except (TypeError, ValueError):
        value = 0.5
    return max(0.0, min(1.0, 1.0 - value))


def score_intents(
    npc: Any,
    needs: Dict[str, float],
    *,
    known: Optional[Dict[str, Any]] = None,
    at_work: bool = False,
    has_work: bool = False,
    can_work_here: bool = False,
    alarm: int = 0,
    current: str = "",
) -> List[Intent]:
    """Score every candidate intent for one NPC. Highest score wins.

    `known` is what this NPC perceived or was told - for example
    `{"ore_missing": True, "saw_player": True}`. Nothing about the rest of the
    world is visible here on purpose.
    """
    known = dict(known or {})
    traits = getattr(npc, "traits", None)
    scored: List[Intent] = []
    for name, spec in INTENTS.items():
        terms: Dict[str, float] = {"base": float(spec["base"])}
        score = float(spec["base"])
        for trait, weight in spec["traits"]:
            contribution = (_trait(traits, trait) - 0.5) * 2.0 * float(weight)
            terms[f"trait:{trait}"] = round(contribution, 4)
            score += contribution
        for need, weight in spec["needs"]:
            contribution = _deficit(needs, need) * float(weight)
            terms[f"need:{need}"] = round(contribution, 4)
            score += contribution
        if name == "work":
            if not has_work:
                continue
            if not can_work_here:
                terms["not_here"] = -0.5
                score -= 0.5
            elif at_work:
                terms["already_here"] = 0.10
                score += 0.10
        if name == "patrol":
            if int(alarm or 0) > 0:
                contribution = 0.30 * min(3, int(alarm)) / 3.0
                terms["alarm"] = round(contribution, 4)
                score += contribution
            else:
                continue
        if name == "investigate":
            if not known.get("something_is_wrong"):
                continue
            terms["known:something_is_wrong"] = 0.60
            score += 0.60
        if name == "socialise" and known.get("player_here"):
            terms["known:player_here"] = 0.10
            score += 0.10
        if name == current:
            terms["stickiness"] = STICKINESS
            score += STICKINESS
        scored.append(Intent(name=name, score=round(score, 4), terms=terms))
    # Total order, so the same world always yields the same decision.
    scored.sort(key=lambda item: (-item.score, item.name))
    return scored


def choose_intent(scored: List[Intent], fallback: str = "rest") -> Intent:
    if not scored:
        return Intent(name=fallback, score=0.0, terms={"fallback": 1.0})
    return scored[0]


def plan_for(
    intent: str,
    *,
    npc: Any,
    location_id: str = "",
    work_location_id: str = "",
) -> List[Dict[str, Any]]:
    """The primitive sequence that carries out an intent.

    A plan is a list of `{action, target_id, params}` records - exactly what an
    ActionRequest needs - never effects and never a state write. The caller
    submits them one at a time through ActionService, so the rules engine gets to
    refuse any of them.
    """
    steps: List[Dict[str, Any]] = []
    if intent == "work" and work_location_id and location_id != work_location_id:
        # "travel" is the routed-destination verb: the same exit checks as a
        # player walking, but the actor that moves is the NPC, not the player.
        steps.append(
            {
                "action": "travel",
                "target_id": work_location_id,
                "params": {"actor_id": getattr(npc, "id", "")},
            }
        )
    elif intent == "eat":
        steps.append({"action": "talk", "target_id": "", "params": {"intent": "eat"}})
    elif intent == "rest":
        steps.append({"action": "wait", "target_id": None, "params": {"minutes": 120}})
    elif intent == "socialise":
        steps.append({"action": "talk", "target_id": "", "params": {"intent": "socialise"}})
    # "patrol" and "investigate" need a target the NPC can actually see, which
    # only the driver knows; it fills one in and this stays empty when it cannot.
    return steps


__all__ = [
    "DEFAULT_NEEDS",
    "INTENTS",
    "Intent",
    "STICKINESS",
    "TRAITS",
    "choose_intent",
    "plan_for",
    "score_intents",
]
