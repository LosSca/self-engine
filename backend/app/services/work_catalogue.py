"""Externally authored works: pick a character from one, or cross into a world.

The player points at a work by name (a novel, a game, a film).  The work is
resolved from *curated local data first* and only falls back to the model when
nothing local matches, because the model's recollection of a given title is
not authoritative and can drift between calls.

Two guards matter here:

* A profile is advisory text for the narrative prompt.  It never writes game
  state, so a hallucinated ability cannot open a door.
* The world adapter turns a profile into a WorldDefinition and validates the
  references before anything is saved, so a broken cross-over cannot be
  persisted as an unplayable world.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.models.work import CharacterProfile, WorkProfile


@dataclass
class WorkEntry:
    """A curated catalogue row.  Keywords are how a fuzzy name resolves."""

    key: str
    profile: Dict[str, Any]
    keywords: List[str] = field(default_factory=list)



DEFAULT_LOCATIONS: List[str] = ["Entrance", "Crossroads", "Depths"]

_CATALOGUE: List[WorkEntry] = [
    WorkEntry(
        key="journey_to_the_west",
        keywords=["journey to the west", "monkey king", "sun wukong", "西游记", "悟空", "唐僧"],
        profile={
            "title": "西游记",
            "medium": "novel",
            "summary": "唐朝僧人受命西行求取真经，一路降妖除魔、收服徒弟。旅途漫长，苦难与玩笑一样多。",
            "tone": "古典神话，诙谐与庄严交织，重旅途与试炼",
            "locations": ["长安城", "五行山", "流沙河", "火焰山", "西天雷音寺"],
            "location_notes": ["城墙下贴着西行的敕令，香火与尘土混在一起。", "压过一尊石猴的山，石缝里还留着褪色的封条。", "河水浑浊得看不见底，岸上有被人踩平的营地。", "热浪把空气烤得扭曲，火舌顺着山脊移动。", "钟声从很远的地方传来，听久了像在耳鸣。"],
            "opening_hint": "你已成为取经队伍的一员，前方是一片不知名的荒野。",
            "characters": [
                {"name": "孙悟空", "role": "大师兄", "description": "自封齐天大圣的石猴，火眼金睛，能辨妖魔，急躁而讲义气。", "traits": ["火眼金睛", "筋斗云", "金箍棒"]},
                {"name": "猪八戒", "role": "二师兄", "description": "天蓬元帅转世，贪吃懒散却肯在关键时刻出力。", "traits": ["九齿钉耙", "贪吃", "力气大"]},
                {"name": "沙悟净", "role": "三师弟", "description": "卷帘大将，沉默可靠，挑担守夜的总是他。", "traits": ["沉稳", "忠诚", "水性"]},
                {"name": "唐三藏", "role": "师父", "description": "意志坚定的僧人，慈悲却不识妖，常成为队伍的软肋。", "traits": ["慈悲", "坚定", "不识妖"]},
            ],
        },
    ),
    WorkEntry(
        key="sherlock_holmes",
        keywords=["sherlock holmes", "福尔摩斯", "baker street", "贝克街", "watson", "华生"],
        profile={
            "title": "福尔摩斯探案集",
            "medium": "novel",
            "summary": "十九世纪伦敦，一位以推理为乐的顾问侦探与他的记录者在雾与煤气灯之间拆解谜案。",
            "tone": "冷静克制，以细节与逻辑取胜，维多利亚时代的潮湿",
            "locations": ["贝克街221B", "苏格兰场", "伦敦东区", "乡间庄园"],
            "location_notes": ["壁炉烧着，桌上摊着昨天的报纸和一瓶没标签的药水。", "走廊里全是烟草与墨水的味道，档案堆到腰高。", "煤气灯照不到的地方，砖墙在渗水。", "雨落在整片草坪上，屋里的钟比别处慢半拍。"],
            "opening_hint": "一封没有署名的信被塞进了门缝，墨迹还带着雨水。",
            "characters": [
                {"name": "夏洛克·福尔摩斯", "role": "顾问侦探", "description": "从鞋底泥土推断出整条街的行走路线，对破案以外的事毫无兴趣。", "traits": ["观察入微", "演绎推理", "化学"]},
                {"name": "约翰·华生", "role": "记录者", "description": "退役军医，把委托人的沉默读出分量，是福尔摩斯不多的朋友。", "traits": ["医学", "可靠", "枪法"]},
                {"name": "雷斯垂德", "role": "警探", "description": "苏格兰场的探长，务实而常被推理绕晕。", "traits": ["坚持", "务实"]},
            ],
        },
    ),
    WorkEntry(
        key="three_body",
        keywords=["three body", "三体", "刘慈欣", "dark forest", "黑暗森林"],
        profile={
            "title": "三体",
            "medium": "novel",
            "summary": "人类文明与三体文明隔着四光年相互打量，猜疑与技术差距把对话逼成了生存博弈。",
            "tone": "冷峻的硬科幻，宏大尺度与个体无力感并存",
            "locations": ["红岸基地", "三体游戏", "太空电梯", "威慑控制室"],
            "location_notes": ["雷达天线在风里缓慢地转，值班室只有打字机的声音。", "一个需要登录的虚拟世界，天空里悬挂着三颗太阳。", "缆索一直伸进云层，脚下是整座发射场。", "所有屏幕都亮着同一个倒计时，没有人说话。"],
            "opening_hint": "监测站的三号天线接收到了一个不来自地球的信号。",
            "characters": [
                {"name": "叶文洁", "role": "天体物理学家", "description": "在红岸基地向宇宙发出了第一条人类信息，信念与创伤同时驱动她。", "traits": ["天体物理", "决绝", "隐忍"]},
                {"name": "罗辑", "role": "面壁者", "description": "被推上前台的社会学家，用最不讲情面的方式理解了黑暗森林。", "traits": ["社会推演", "冷静", "不择手段"]},
                {"name": "史强", "role": "警官", "description": "粗粝务实的老刑警，在末日面前依然只信手上的活儿。", "traits": ["嗅觉敏锐", "枪械", "街头智慧"]},
            ],
        },
    ),
    WorkEntry(
        key="wandering_earth",
        keywords=["wandering earth", "流浪地球", "行星发动机"],
        profile={
            "title": "流浪地球",
            "medium": "novel",
            "summary": "太阳即将膨胀，人类给地球装上发动机，开始一场持续两千五百年的逃亡。",
            "tone": "宏大工程叙事，个体渺小但必须完成使命",
            "locations": ["行星发动机控制中心", "地下城", "地表补给站", "领航空间站"],
            "location_notes": ["控制台微微震动，指示灯把它们映成红色。", "人群在低矮的灯带下排队，空气里有金属和油烟味。", "零下八十度，防护门的密封条结着厚霜。", "舷窗外的地球是一个带着蓝色尾迹的点。"],
            "opening_hint": "发动机的震动透过地板传来，今天的值班表上多了一个陌生的名字。",
            "characters": [
                {"name": "刘启", "role": "工程师", "description": "在地下城长大的年轻人，熟悉发动机的每一根管线。", "traits": ["机械", "叛逆", "熟悉地下管网"]},
                {"name": "韩朵朵", "role": "学生", "description": "把广播当作最后一种武器的人，声音倔强。", "traits": ["表达", "坚持"]},
            ],
        },
    ),
    WorkEntry(
        key="harry_potter",
        keywords=["harry potter", "哈利波特", "霍格沃茨", "hogwarts", "魔法石"],
        profile={
            "title": "哈利·波特",
            "medium": "novel",
            "summary": "十一岁男孩得知自己是巫师，走进一座连楼梯都会动的学校，与朋友的友谊和潜伏的阴影一同生长。",
            "tone": "成长与悬疑并行，学院日常底下压着更古老的东西",
            "locations": ["霍格沃茨", "对角巷", "禁林", "有求必应屋"],
            "location_notes": ["楼梯在你脚下换了个方向，画像们正在交头接耳。", "狭窄的巷子两侧堆满坩埚与羽毛笔，招牌在无声地转。", "树影太密，月光只落得到几个点上。", "门只在你真的需要它的时候出现。"],
            "opening_hint": "猫头鹰落在窗台上，爪子下压着一封用绿色墨水写成的信。",
            "characters": [
                {"name": "哈利·波特", "role": "学生", "description": "额头有道闪电伤疤，命运早在婴儿时期就替他做了选择。", "traits": ["飞行", "守护神咒", "勇气"]},
                {"name": "赫敏·格兰杰", "role": "学生", "description": "把每一本书都读完再开口，靠准备和逻辑赢下大多数危机。", "traits": ["博学", "魔药", "逻辑"]},
                {"name": "罗恩·韦斯莱", "role": "学生", "description": "在兄长与朋友的阴影里学会了忠诚，棋下得极好。", "traits": ["巫师棋", "义气", "幽默"]},
            ],
        },
    ),
    WorkEntry(
        key="lotr",
        keywords=["lord of the rings", "魔戒", "指环王", "middle earth", "中土"],
        profile={
            "title": "指环王",
            "medium": "novel",
            "summary": "一枚戒指承载着足以腐化世界的重量，被托付给最不该承担它的人，送往它诞生的地方。",
            "tone": "史诗与旅途，苍凉但不绝望",
            "locations": ["夏尔", "布理", "摩瑞亚", "末日火山"],
            "location_notes": ["青草、烟斗和一条不该有人急匆匆走的路。", "客栈里人声嘈杂，角落里的斗篷人一直没抬头。", "黑暗中只有回声，深得不像走廊。", "空气烫得能看见扭曲，远处有红光在缓慢地涌。"],
            "opening_hint": "甘道夫在门外停下，把他的烟斗磕灭了。",
            "characters": [
                {"name": "弗罗多·巴金斯", "role": "持戒人", "description": "最普通也最坚韧的霍比特人，戒指的重量一天比一天沉。", "traits": ["坚韧", "隐忍", "同情心"]},
                {"name": "山姆·甘姆吉", "role": "园丁", "description": "只答应过一句话，就一直走到最后。", "traits": ["忠诚", "耐力", "烹饪"]},
                {"name": "阿拉贡", "role": "游侠", "description": "流亡王族的后裔，在成为王之前先学会做一名向导。", "traits": ["剑术", "导航", "领导力"]},
            ],
        },
    ),
]


def _normalise(text: str) -> str:
    return " ".join((text or "").strip().lower().replace("·", "").split())


def catalogue_keys() -> List[str]:
    return [entry.key for entry in _CATALOGUE]


def _match_entry(title: str) -> Optional[WorkEntry]:
    """Exact key, then keyword containment.  Nothing fuzzy beyond that."""
    wanted = _normalise(title)
    if not wanted:
        return None
    for entry in _CATALOGUE:
        if _normalise(entry.key) == wanted:
            return entry
    for entry in _CATALOGUE:
        for keyword in entry.keywords:
            key = _normalise(keyword)
            if key and key in wanted:
                return entry
    return None


def find_work(title: str) -> Optional[WorkProfile]:
    entry = _match_entry(title)
    if entry is None:
        return None
    return WorkProfile.model_validate(copy.deepcopy(entry.profile))


def key_for(title: str) -> str:
    """Stable ASCII key for a work title.

    Catalogue hits map back to their entry key ("福尔摩斯" -> "sherlock_holmes")
    so generated ids stay stable across languages.  A title the catalogue does
    not know falls back to slug_of, which yields "entry" for pure-CJK titles --
    callers are expected to make that unique.
    """
    title = (title or "").strip()
    if not title:
        return "entry"
    entry = _match_entry(title)
    if entry is not None:
        return entry.key
    return slug_of(title)


def profile_from_payload(payload: Any, title: str = "", source: str = "generated") -> WorkProfile:
    """Validate model output into a profile, filling in what it omitted."""
    if not isinstance(payload, dict):
        raise ValueError("模型没有返回作品对象")
    data = dict(payload)
    data["title"] = str(data.get("title") or title or "").strip()
    if not data["title"]:
        raise ValueError("作品名为空")
    characters = data.get("characters") or []
    if not isinstance(characters, list):
        raise ValueError("characters 必须是列表")
    cleaned = []
    for item in characters:
        if not isinstance(item, dict):
            continue
        if not str(item.get("name") or "").strip():
            continue
        cleaned.append(item)
    if not cleaned:
        raise ValueError("作品没有可用角色")
    data["characters"] = cleaned
    locations = [str(item).strip() for item in (data.get("locations") or []) if str(item).strip()]
    data["locations"] = locations or DEFAULT_LOCATIONS
    notes = [str(item).strip() for item in (data.get("location_notes") or [])]
    data["location_notes"] = (notes + [""] * len(data["locations"]))[: len(data["locations"])]
    data["source"] = source
    return WorkProfile.model_validate(data)


def character_catalogue(profile: WorkProfile) -> List[Dict[str, Any]]:
    """Flatten a profile into the rows a picker needs."""
    return [
        {
            "name": character.name,
            "role": character.role,
            "description": character.description,
            "traits": list(character.traits),
            "source_work": profile.title,
        }
        for character in profile.characters
    ]


@dataclass
class WorldSeed:
    world_id: str
    name: str
    summary: str
    start_location_id: str
    locations: List[Dict[str, str]]
    character: CharacterProfile


def seed_world(profile: WorkProfile, character: CharacterProfile, world_id: str) -> WorldSeed:
    """Base world skeleton for a cross-over.  No doors, no items, no quests. A
    cross-over world is a *skeleton*: it gives the model a coherent map to
    grow into, and deliberately does not invent rules it cannot ground."""
    names = [name for name in profile.locations if str(name).strip()] or ["Entrance", "Crossroads", "Depths"]
    seen = set()
    locations = []
    for index, name in enumerate(names):
        lid = unique_id(name, seen, f"loc{index + 1}")
        seen.add(lid)
        locations.append({"name": name, "id": lid})
    return WorldSeed(
        world_id=world_id,
        name=profile.title,
        summary=profile.summary or f"{profile.title}的世界。",
        start_location_id=locations[0]["id"],
        locations=locations,
        character=character,
    )


def slug_of(text: str, fallback: str = "entry") -> str:
    """ASCII-only id: an id may be a URL path segment, so Chinese names get
    a positional fallback rather than percent-encoded noise."""
    cleaned = "".join(ch if ch.isascii() and ch.isalnum() else "_" for ch in str(text).lower())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    if not cleaned or not cleaned[0].isalpha():
        return fallback
    return cleaned[:40]


def unique_id(base: str, taken: set, fallback: str) -> str:
    """Stable unique id: base, base_2, base_3, ..."""
    slug = slug_of(base, fallback)
    if slug not in taken:
        return slug
    index = 2
    while f"{slug}_{index}" in taken:
        index += 1
    return f"{slug}_{index}"


def seed_world_definition(
    profile: WorkProfile,
    character: CharacterProfile,
    world_id: str,
    scenario_id: str = "",
) -> "WorldDefinition":
    """A connected, playable skeleton: every location reachable from the start.

    The spine is a chain (loc1 -> loc2 -> ... -> locN) with a return exit at
    each step, so the world passes reference validation and the player can walk
    it.  Regions are left empty because a cross-over author usually wants to
    set them by hand later.
    """
    from app.models.game import (
        EntityDef,
        ExitDef,
        LocationDef,
        WorldDefinition,
    )

    seed = seed_world(profile, character, world_id)
    entries = seed.locations
    locations = []
    for index, entry in enumerate(entries):
        exits = []
        if index > 0:
            exits.append(ExitDef(target_id=entries[index - 1]["id"], label=f"返回{entries[index - 1]['name']}"))
        if index + 1 < len(entries):
            exits.append(ExitDef(target_id=entries[index + 1]["id"], label=f"前往{entries[index + 1]['name']}"))
        locations.append(
            LocationDef(
                id=entry["id"],
                name=entry["name"],
                description=(profile.location_notes or [])[index] if index < len(profile.location_notes or []) else f"{profile.title}中的一处所在。",
                exits=exits,
            )
        )

    entity = EntityDef(
        id=unique_id(character.name, set(), "guide"),
        name=character.name,
        description=character.description or character.role,
        location_id=seed.start_location_id,
        kind="npc",
        tags=list(character.traits),
    )
    return WorldDefinition(
        world_id=world_id,
        scenario_id=scenario_id,
        name=seed.name,
        description=seed.summary,
        start_location_id=seed.start_location_id,
        locations=locations,
        entities=[entity],
    )


def opening_hint(profile: WorkProfile, character: CharacterProfile) -> str:
    """One line woven from the profile, used to seed the first turn."""
    hint = (profile.opening_hint or "").strip()
    who = f"你以{character.name}的身份站在这里。"
    return f"{hint} {who}".strip()