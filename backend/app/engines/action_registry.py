"""Declarative catalogue of the actions the rules engine can decide.

The catalogue is the single source of truth for *what an action is*: its name,
its player-facing label, whether it needs a target, which rule method decides it
and which words the text parser maps onto it. Adding a verb means adding one
ActionSpec here plus one rule method; nothing else has to learn about it, and
the parser, the API and the docs all read the same table.

The catalogue deliberately holds no code and no behaviour: configuration can
describe an action, but it can never inject a new implementation at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

#: How much of a target the action needs.
TargetNeed = Literal["none", "required", "optional"]


@dataclass(frozen=True)
class ActionSpec:
    """One gameplay verb the rule engine knows how to decide."""

    name: str
    label: str
    handler: str
    target: TargetNeed = "none"
    description: str = ""
    #: Words the deterministic text parser maps onto this action. Longest
    #: phrases are matched first, so "打开" wins over "打".
    verbs: Tuple[str, ...] = ()
    #: Lower runs first when two verbs could both match a phrase.
    verb_priority: int = 100
    #: In-game minutes the verb consumes. 0 means the action takes no time
    #: (looking around, talking); None means "ask game_clock for the default".
    minutes: Optional[int] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "label": self.label,
            "target": self.target,
            "description": self.description,
            "verbs": list(self.verbs),
            "minutes": self.minutes,
        }


_SPECS: Dict[str, ActionSpec] = {}
#: Registration order is stable, so the parser table and the API are too.
_ORDER: List[str] = []


def register_action(spec: ActionSpec) -> ActionSpec:
    """Add (or replace) one action in the catalogue."""
    if spec.name in _SPECS:
        _ORDER.remove(spec.name)
    _SPECS[spec.name] = spec
    _ORDER.append(spec.name)
    return spec


def get_action(name: str) -> Optional[ActionSpec]:
    return _SPECS.get(str(name))


def action_names() -> Tuple[str, ...]:
    return tuple(_ORDER)


def all_actions() -> List[ActionSpec]:
    return [_SPECS[name] for name in _ORDER]


def verb_table() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """(action, verbs) pairs ordered by verb_priority, longest phrase first."""
    specs = sorted(all_actions(), key=lambda spec: (spec.verb_priority, _ORDER.index(spec.name)))
    # Verb order inside one action is authored, not computed: Chinese phrases
    # cannot be ordered by character count ("打开" is shorter than "unlock" but
    # must be tried first), so each spec lists its own longest-first sequence.
    return tuple((spec.name, tuple(spec.verbs)) for spec in specs if spec.verbs)


register_action(
    ActionSpec(
        name="attack",
        label="攻击",
        handler="_attack",
        target="required",
        description="Strike a creature or hostile NPC that is in the current location.",
        minutes=1,
        verbs=("攻击", "打击", "砍", "劈", "刺向", "刺", "揍", "attack", "strike",
               "fight", "kill", "stab", "slash", "hit the", "hit "),
        verb_priority=10,
    )
)
register_action(
    ActionSpec(
        name="flee",
        label="逃跑",
        handler="_flee",
        target="optional",
        description="Leave the current fight; the enemy may still land a parting blow.",
        minutes=1,
        verbs=("逃跑", "逃走", "撤离", "撤退", "跑", "flee", "run away", "retreat", "escape"),
        verb_priority=20,
    )
)
register_action(
    ActionSpec(
        name="defend",
        label="防御",
        handler="_defend",
        target="optional",
        description="Raise your guard for one round instead of attacking.",
        minutes=1,
        verbs=("防御", "格挡", "防守", "架起", "defend", "block", "guard", "parry"),
        verb_priority=30,
    )
)
register_action(
    ActionSpec(
        name="take",
        label="拾取",
        handler="_take",
        minutes=1,
        target="required",
        description="Pick up an item that lies in the current location.",
        verbs=("拾取", "捡起", "拿走", "拿起", "拿到", "拿", "拿取", "take", "pick up", "grab"),
        verb_priority=40,
    )
)
register_action(
    ActionSpec(
        name="open",
        label="打开",
        handler="_open",
        minutes=1,
        target="required",
        description="Open a door here, if it is unlocked or you carry its key.",
        verbs=("打开", "开启", "推开", "开锁", "开", "open", "unlock"),
        verb_priority=50,
    )
)
register_action(
    ActionSpec(
        name="use",
        label="使用",
        handler="_use",
        target="required",
        description="Apply an item you carry.",
        verbs=("使用", "用", "use", "apply"),
        verb_priority=60,
    )
)
register_action(
    ActionSpec(
        name="talk",
        label="交谈",
        handler="_talk",
        minutes=0,
        target="optional",
        description="Speak to someone here.",
        verbs=("交谈", "对话", "说话", "说说", "问", "说", "talk", "speak", "ask"),
        verb_priority=70,
    )
)
register_action(
    ActionSpec(
        name="move",
        label="移动",
        handler="_move",
        minutes=10,
        target="required",
        description="Walk through an exit to a neighbouring location.",
        verbs=("前往", "移动到", "走到", "进入", "回到", "去往", "去", "go to", "head to",
               "travel to", "move to", "walk to", "enter"),
        verb_priority=80,
    )
)
register_action(
    ActionSpec(
        name="equip",
        label="装备",
        handler="_equip",
        target="required",
        description="Equip an item you carry into its authored slot.",
        verbs=("装备", "穿上", "穿戴", "equip", "wield", "wear"),
        verb_priority=25,
    )
)
register_action(
    ActionSpec(
        name="unequip",
        label="卸下",
        handler="_unequip",
        target="optional",
        description="Remove an equipped item by slot or item id.",
        verbs=("卸下", "脱下", "unequip", "remove armor", "take off"),
        verb_priority=26,
    )
)
register_action(
    ActionSpec(
        name="buy",
        label="购买",
        handler="_buy",
        target="required",
        description="Buy an item from a merchant here.",
        verbs=("购买", "买", "buy", "purchase"),
        verb_priority=27,
    )
)
register_action(
    ActionSpec(
        name="sell",
        label="出售",
        handler="_sell",
        target="required",
        description="Sell a carried item to a merchant here.",
        verbs=("出售", "卖掉", "卖", "sell"),
        verb_priority=28,
    )
)
register_action(
    ActionSpec(
        name="choose",
        label="选择",
        handler="_choose",
        target="required",
        description="Resolve the pending authored choice with one of its option ids.",
        verbs=(),
        verb_priority=210,
    )
)
register_action(
    ActionSpec(
        name="custom",
        label="自定义行动",
        handler="_custom",
        target="optional",
        description=(
            "An action the world itself declares (conditions plus effects). "
            "The world supplies the verbs, so this entry declares none."
        ),
        verbs=(),
        verb_priority=200,
    )
)
register_action(
    ActionSpec(
        name="investigate",
        label="调查",
        handler="_investigate",
        minutes=5,
        target="optional",
        description="Inspect one entity, item or door here, or the room when no target is named.",
        verbs=("调查", "检查", "查看", "端详", "inspect", "examine", "investigate"),
        verb_priority=85,
    )
)
register_action(
    ActionSpec(
        name="look",
        label="观察",
        handler="_look",
        minutes=0,
        target="none",
        description="Describe the current location, its items, doors and exits.",
        verbs=("观察", "环顾", "看看", "看", "look", "observe", "survey"),
        verb_priority=90,
    )
)
