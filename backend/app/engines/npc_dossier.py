"""NPC dossiers: stable identity plus run-time state, without a model each (Phase 2).

An NPC is *data*, not an agent. Its dossier is assembled from what already
exists, so nothing is stored twice:

* authored configuration comes from ``EntityDef`` (name, description, tags,
  faction, shop, opening lines, attributes);
* run-time facts come from ``EntityState`` (location, hp, relations, alert)
  and from the campaign flags the game engine already writes;
* opinions, moods and secrets come from the existing ``NpcMind`` thoughts,
  which are keyed by thought name (``feeling``, ``mood``, ``goal``,
  ``opinion_of_player``, ``secret_plan`` and anything else authored or
  inferred).

The same dossier feeds the narrative agent, so two NPCs in the same scene are
never given the same context. Knowledge isolation is explicit: ``knows``
lists only what this entity was given, and ``secrets`` are only visible
because they are *its* secrets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.models.game import EntityDef, EntityState, GameState, WorldDefinition

#: Thought keys that describe how the NPC currently feels or what it wants.
MOOD_KEYS = ("feeling", "mood", "emotion")
GOAL_KEYS = ("goal", "objective", "secret_plan")
OPINION_KEYS = ("opinion_of_player", "opinion", "attitude")
#: Thought prefixes the engine may use for things an NPC came to know.
KNOWLEDGE_PREFIXES = ("knows:", "knows_", "learned:")
SECRET_PREFIXES = ("secret:", "secret_", "hides:")


@dataclass
class NpcDossier:
    """Everything the agents may know about one NPC in this campaign."""

    entity_id: str
    name: str
    kind: str = "npc"
    description: str = ""
    personality: List[str] = field(default_factory=list)
    background: List[str] = field(default_factory=list)
    motivation: List[str] = field(default_factory=list)
    goals: List[str] = field(default_factory=list)
    emotional_state: Dict[str, str] = field(default_factory=dict)
    relationships: Dict[str, int] = field(default_factory=dict)
    #: Things this NPC was explicitly given (dialogue, shop, authored notes).
    knows: List[str] = field(default_factory=list)
    #: Only this NPC's own secrets; never shared with another dossier.
    secrets: List[str] = field(default_factory=list)
    current_status: Dict[str, Any] = field(default_factory=dict)
    merchant: bool = False

    def identity_lines(self) -> List[str]:
        lines = [f"{self.name} ({self.kind})"]
        if self.description:
            lines.append(self.description)
        lines.extend(self.background)
        if self.personality:
            lines.append("Personality: " + "; ".join(self.personality))
        if self.motivation:
            lines.append("Motivation: " + "; ".join(self.motivation))
        if self.goals:
            lines.append("Goals: " + "; ".join(self.goals))
        return lines

    def state_lines(self) -> List[str]:
        lines: List[str] = []
        for key, value in self.emotional_state.items():
            lines.append(f"{key}: {value}")
        for who, value in self.relationships.items():
            lines.append(f"attitude toward {who}: {value}")
        for key, value in self.current_status.items():
            lines.append(f"{key}: {value}")
        return lines

    def knowledge_lines(self, world: Optional[WorldDefinition] = None) -> List[str]:
        return list(self.knows)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "personality": list(self.personality),
            "background": list(self.background),
            "motivation": list(self.motivation),
            "goals": list(self.goals),
            "emotional_state": dict(self.emotional_state),
            "relationships": dict(self.relationships),
            "knows": list(self.knows),
            "secrets": list(self.secrets),
            "current_status": dict(self.current_status),
            "merchant": self.merchant,
        }

    @classmethod
    def from_entity(cls, entity: EntityDef, *, mind: Any = None) -> "NpcDossier":
        """A dossier from an authored entity alone.

        Used by the streaming play loop, which holds the scenario catalogue but
        not a ``GameState`` snapshot at prompt-build time. Run-time state is
        omitted rather than guessed: an invented location or mood is worse than
        a missing one, because the narrator would treat it as fact.
        """
        attributes = entity.attributes or {}
        dossier = cls(
            entity_id=entity.id,
            name=entity.name or entity.id,
            kind=entity.kind,
            description=entity.description or "",
            merchant=bool(entity.shop_items),
        )
        personality = attributes.get("personality")
        if isinstance(personality, str):
            dossier.personality = _split(personality)
        elif isinstance(personality, (list, tuple)):
            dossier.personality = [str(item) for item in personality if str(item).strip()]
        background = attributes.get("background")
        if isinstance(background, str) and background.strip():
            dossier.background.append(background.strip())
        motivation = attributes.get("motivation")
        if isinstance(motivation, str):
            dossier.motivation = _split(motivation)
        elif isinstance(motivation, (list, tuple)):
            dossier.motivation = [str(item) for item in motivation if str(item).strip()]
        if entity.faction:
            dossier.background.append(f"Faction: {entity.faction}")
        if entity.shop_items:
            dossier.knows.append(
                "sells: " + ", ".join(str(item) for item in entity.shop_items)
            )
        if mind is not None:
            _apply_thoughts(dossier, mind)
        return dossier


def _split(value: str) -> List[str]:
    return [part.strip() for part in str(value or "").replace("|", ";").split(";") if part.strip()]


def _matching(thoughts: Dict[str, Any], keys) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, thought in thoughts.items():
        if key in keys:
            out[key] = str(getattr(thought, "value", thought))
    return out


def _prefixed(thoughts: Dict[str, Any], prefixes) -> List[str]:
    out: List[str] = []
    for key, thought in thoughts.items():
        if any(key.startswith(prefix) for prefix in prefixes):
            out.append(str(getattr(thought, "value", thought)))
    return out


def _apply_thoughts(dossier: NpcDossier, mind: Any) -> None:
    """Fold one NPC's own thoughts into their dossier.

    Only this NPC's thoughts are passed in, which is the whole isolation
    mechanism: another NPC's secrets are simply not in this call.
    """
    thoughts = getattr(mind, "thoughts", None) or {}
    if not thoughts:
        return
    dossier.emotional_state = _matching(thoughts, MOOD_KEYS)
    dossier.goals = list(_matching(thoughts, GOAL_KEYS).values())
    dossier.motivation.extend(
        value for value in _matching(thoughts, GOAL_KEYS).values()
        if value not in dossier.motivation
    )
    dossier.relationships.update(_matching(thoughts, OPINION_KEYS))
    for key, thought in thoughts.items():
        value = str(getattr(thought, "value", "") or "").strip()
        if not value:
            continue
        lowered = key.lower()
        if lowered.startswith(SECRET_PREFIXES):
            dossier.secrets.append(value)
        elif lowered.startswith(KNOWLEDGE_PREFIXES):
            dossier.knows.append(value)


def dossier_for(
    world: WorldDefinition,
    state: GameState,
    entity_id: str,
    *,
    mind: Any = None,
) -> Optional[NpcDossier]:
    """Assemble one NPC's dossier from the world and the campaign state.

    ``mind`` is an optional ``NpcMind``-shaped object (``.thoughts`` dict of
    key -> object with ``.value``); when absent the dossier still describes
    the stable identity.
    """
    entity: Optional[EntityDef] = None
    for candidate in world.entities:
        if candidate.id == entity_id:
            entity = candidate
            break
    if entity is None:
        return None
    attributes = entity.attributes or {}
    dossier = NpcDossier(
        entity_id=entity.id,
        name=entity.name or entity.id,
        kind=entity.kind,
        description=entity.description or "",
        merchant=bool(entity.shop_items),
    )
    personality = attributes.get("personality")
    if isinstance(personality, str):
        dossier.personality = _split(personality)
    elif isinstance(personality, (list, tuple)):
        dossier.personality = [str(item) for item in personality if str(item).strip()]
    background = attributes.get("background")
    if isinstance(background, str) and background.strip():
        dossier.background.append(background.strip())
    motivation = attributes.get("motivation")
    if isinstance(motivation, str):
        dossier.motivation = _split(motivation)
    elif isinstance(motivation, (list, tuple)):
        dossier.motivation = [str(item) for item in motivation if str(item).strip()]
    if entity.faction:
        dossier.background.append(f"Faction: {entity.faction}")
    if entity.shop_items:
        dossier.knows.append(
            "sells: " + ", ".join(str(item) for item in entity.shop_items)
        )

    record: Optional[EntityState] = state.entities.get(entity.id)
    if record is not None:
        if record.location_id:
            dossier.current_status["location"] = record.location_id
        dossier.current_status["hp"] = f"{record.hp}/{record.max_hp}"
        dossier.current_status["alive"] = bool(record.alive)
        if record.alert:
            dossier.current_status["alert"] = record.alert
        dossier.relationships = dict(record.relations or {})

    thoughts = getattr(mind, "thoughts", None) or {}
    if thoughts:
        dossier.emotional_state = _matching(thoughts, MOOD_KEYS)
        dossier.goals = list(_matching(thoughts, GOAL_KEYS).values())
        dossier.motivation.extend(
            value for value in _matching(thoughts, GOAL_KEYS).values()
            if value not in dossier.motivation
        )
        dossier.knows.extend(_prefixed(thoughts, KNOWLEDGE_PREFIXES))
        dossier.secrets.extend(_prefixed(thoughts, SECRET_PREFIXES))
        opinions = _matching(thoughts, OPINION_KEYS)
        if opinions:
            dossier.relationships.setdefault(
                "player", " ".join(f"{k}={v}" for k, v in opinions.items())
            )
    return dossier
