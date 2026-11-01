import sqlite3
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from threading import Lock

from app.models.knowledge import (
    KnowledgeEntry,
    KnowledgeEntryCreate,
    KnowledgeEntryPatch,
    ModelPreset,
    ModelPresetCreate,
    ModelPresetPatch,
    PlayConfig,
    Storyline,
)


class StoryCardType(str, Enum):
    NPC = "NPC"
    LOCATION = "LOCATION"
    FACTION = "FACTION"
    ITEM = "ITEM"
    LORE = "LORE"


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    tone_instructions: str
    opening_narrative: str
    language: str
    lore_text: str
    created_at: str
    setup_questions: list = field(default_factory=list)
    opening_mode: str = "fixed"  # "fixed" | "ai"
    ai_opening_directive: str = ""
    world_type: str = ""  # preset key chosen on the home screen ("fantasy", ...)


@dataclass
class StoryCard:
    id: str
    scenario_id: str
    card_type: StoryCardType
    name: str
    content: dict
    created_at: str


@dataclass
class Campaign:
    id: str
    scenario_id: str
    player_name: str
    created_at: str
    setup_answers: dict = field(default_factory=dict)
    generated_opening: str = ""
    combat_enabled: bool = True
    character_picks: dict = field(default_factory=dict)


@dataclass
class CharacterLook:
    id: str
    character_id: str
    outfit: str
    expression: str
    image_url: str = ""
    sort_order: int = 0


@dataclass
class ScenarioCharacter:
    id: str
    scenario_id: str
    name: str
    role: str = ""
    description: str = ""
    portrait_url: str = ""
    is_player: bool = False
    created_at: str = ""
    looks: list = field(default_factory=list)

    def look_for(self, outfit: str, expression: str) -> "CharacterLook | None":
        """Exact outfit+expression match, then outfit-only fallback."""
        for look in self.looks:
            if look.outfit == outfit and look.expression == expression:
                return look
        for look in self.looks:
            if look.outfit == outfit and look.image_url:
                return look
        return None


class ScenarioStore:
    SCHEMA_VERSION = 12

    _MIGRATIONS = {
        1: [
            """CREATE TABLE IF NOT EXISTS scenarios (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                tone_instructions TEXT NOT NULL DEFAULT '',
                opening_narrative TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT 'en',
                lore_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS story_cards (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL REFERENCES scenarios(id),
                card_type TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL REFERENCES scenarios(id),
                player_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
        ],
        2: [
            "ALTER TABLE scenarios ADD COLUMN setup_questions TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE campaigns ADD COLUMN setup_answers TEXT NOT NULL DEFAULT '{}'",
        ],
        3: [
            "ALTER TABLE scenarios ADD COLUMN opening_mode TEXT NOT NULL DEFAULT 'fixed'",
            "ALTER TABLE scenarios ADD COLUMN ai_opening_directive TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE campaigns ADD COLUMN generated_opening TEXT NOT NULL DEFAULT ''",
        ],
        4: [
            "ALTER TABLE campaigns ADD COLUMN combat_enabled INTEGER NOT NULL DEFAULT 1",
        ],
        5: [
            """CREATE TABLE IF NOT EXISTS scenario_characters (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL REFERENCES scenarios(id),
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                portrait_url TEXT NOT NULL DEFAULT '',
                is_player INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS character_looks (
                id TEXT PRIMARY KEY,
                character_id TEXT NOT NULL REFERENCES scenario_characters(id),
                outfit TEXT NOT NULL DEFAULT '默认',
                expression TEXT NOT NULL DEFAULT '平静',
                image_url TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0
            )""",
            "ALTER TABLE campaigns ADD COLUMN character_json TEXT NOT NULL DEFAULT '{}'",
            "CREATE INDEX IF NOT EXISTS idx_scenario_characters_scenario "
            "ON scenario_characters(scenario_id)",
            "CREATE INDEX IF NOT EXISTS idx_character_looks_character "
            "ON character_looks(character_id)",
        ],
        6: [
            "ALTER TABLE scenarios ADD COLUMN world_type TEXT NOT NULL DEFAULT ''",
        ],
        8: [
            "ALTER TABLE scenario_play_config ADD COLUMN context_budget_chars INTEGER NOT NULL DEFAULT 0",
        ],
        9: [
            "ALTER TABLE scenario_knowledge ADD COLUMN audience_npc_ids TEXT NOT NULL DEFAULT '[]'",
        ],
        10: [
            "ALTER TABLE scenario_play_config ADD COLUMN intent_preset_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE scenario_play_config ADD COLUMN narrator_prompt TEXT NOT NULL DEFAULT ''",
        ],
        11: [
            "ALTER TABLE scenario_play_config ADD COLUMN dialogue_preset_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE scenario_play_config ADD COLUMN summary_preset_id TEXT NOT NULL DEFAULT ''",
        ],
        12: [
            "ALTER TABLE model_presets ADD COLUMN extras_json TEXT NOT NULL DEFAULT '{}'",
        ],
        7: [
            """CREATE TABLE IF NOT EXISTS scenario_knowledge (
                id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL REFERENCES scenarios(id),
                layer TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                depth INTEGER NOT NULL DEFAULT 5,
                always_on INTEGER NOT NULL DEFAULT 0,
                mode_scope TEXT NOT NULL DEFAULT 'both',
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS scenario_play_config (
                scenario_id TEXT PRIMARY KEY REFERENCES scenarios(id),
                mode TEXT NOT NULL DEFAULT 'free',
                storyline_json TEXT NOT NULL DEFAULT '{}',
                max_depth INTEGER NOT NULL DEFAULT 9,
                model_preset_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS model_presets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'narrator',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                temperature REAL NOT NULL DEFAULT 0.8,
                max_tokens INTEGER NOT NULL DEFAULT 800,
                notes TEXT NOT NULL DEFAULT '',
                is_builtin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_scenario_knowledge_scenario "
            "ON scenario_knowledge(scenario_id)",
        ],
    }

    def __init__(self, db_path: str = "scenarios.db"):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = Lock()
        self._migrate()
        self._seed_builtin_presets()

    def _get_schema_version(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError:
            return 0

    def _migrate(self):
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = self._get_schema_version()
            for version in sorted(self._MIGRATIONS.keys()):
                if version <= current:
                    continue
                for sql in self._MIGRATIONS[version]:
                    self._conn.execute(sql)
                self._conn.execute(
                    "INSERT INTO schema_version VALUES (?, ?)",
                    (version, datetime.utcnow().isoformat()),
                )
            self._conn.commit()

    _SCENARIO_COLS = (
        "id, title, description, tone_instructions, opening_narrative, "
        "language, lore_text, created_at, setup_questions, "
        "opening_mode, ai_opening_directive, world_type"
    )

    def create_scenario(
        self,
        title: str,
        description: str = "",
        tone_instructions: str = "",
        opening_narrative: str = "",
        language: str = "en",
        lore_text: str = "",
        setup_questions: list | None = None,
        opening_mode: str = "fixed",
        ai_opening_directive: str = "",
        world_type: str = "",
    ) -> Scenario:
        scenario = Scenario(
            id=str(uuid.uuid4()),
            title=title,
            description=description,
            tone_instructions=tone_instructions,
            opening_narrative=opening_narrative,
            language=language,
            lore_text=lore_text,
            created_at=datetime.utcnow().isoformat(),
            setup_questions=setup_questions or [],
            opening_mode=opening_mode if opening_mode in ("fixed", "ai") else "fixed",
            ai_opening_directive=ai_opening_directive,
            world_type=world_type or "",
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO scenarios ({self._SCENARIO_COLS}) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (scenario.id, scenario.title, scenario.description,
                 scenario.tone_instructions, scenario.opening_narrative,
                 scenario.language, scenario.lore_text, scenario.created_at,
                 json.dumps(scenario.setup_questions),
                 scenario.opening_mode, scenario.ai_opening_directive,
                 scenario.world_type),
            )
            self._conn.commit()
        return scenario

    @staticmethod
    def _row_to_scenario(row) -> Scenario:
        # Columns (in order matching _SCENARIO_COLS):
        #   id, title, description, tone_instructions, opening_narrative,
        #   language, lore_text, created_at, setup_questions(JSON),
        #   opening_mode, ai_opening_directive
        try:
            setup_questions = json.loads(row[8]) if row[8] else []
        except (json.JSONDecodeError, TypeError):
            setup_questions = []
        opening_mode = row[9] if len(row) > 9 and row[9] else "fixed"
        ai_directive = row[10] if len(row) > 10 and row[10] is not None else ""
        world_type = row[11] if len(row) > 11 and row[11] is not None else ""
        return Scenario(
            id=row[0], title=row[1], description=row[2],
            tone_instructions=row[3], opening_narrative=row[4],
            language=row[5], lore_text=row[6], created_at=row[7],
            setup_questions=setup_questions,
            opening_mode=opening_mode,
            ai_opening_directive=ai_directive,
            world_type=world_type,
        )

    def get_scenario(self, scenario_id: str) -> "Scenario | None":
        row = self._conn.execute(
            f"SELECT {self._SCENARIO_COLS} FROM scenarios WHERE id=?", (scenario_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_scenario(row)

    def list_scenarios(self) -> "list[Scenario]":
        rows = self._conn.execute(
            f"SELECT {self._SCENARIO_COLS} FROM scenarios ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_scenario(r) for r in rows]

    def add_story_card(
        self,
        scenario_id: str,
        card_type: StoryCardType,
        name: str,
        content: dict,
    ) -> StoryCard:
        card = StoryCard(
            id=str(uuid.uuid4()),
            scenario_id=scenario_id,
            card_type=card_type,
            name=name,
            content=content,
            created_at=datetime.utcnow().isoformat(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO story_cards VALUES (?,?,?,?,?,?)",
                (card.id, card.scenario_id, card.card_type.value,
                 card.name, json.dumps(card.content), card.created_at),
            )
            self._conn.commit()
        return card

    def get_story_cards(self, scenario_id: str) -> "list[StoryCard]":
        rows = self._conn.execute(
            "SELECT * FROM story_cards WHERE scenario_id=? ORDER BY created_at ASC",
            (scenario_id,),
        ).fetchall()
        return [
            StoryCard(r[0], r[1], StoryCardType(r[2]), r[3], json.loads(r[4]), r[5])
            for r in rows
        ]

    _CAMPAIGN_COLS = (
        "id, scenario_id, player_name, created_at, setup_answers, "
        "generated_opening, combat_enabled, character_json"
    )

    def create_campaign(self, scenario_id: str, player_name: str) -> Campaign:
        campaign = Campaign(
            id=str(uuid.uuid4()),
            scenario_id=scenario_id,
            player_name=player_name,
            created_at=datetime.utcnow().isoformat(),
            setup_answers={},
            generated_opening="",
            combat_enabled=True,
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO campaigns ({self._CAMPAIGN_COLS}) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (campaign.id, campaign.scenario_id, campaign.player_name,
                 campaign.created_at, json.dumps(campaign.setup_answers),
                 campaign.generated_opening, 1 if campaign.combat_enabled else 0,
                 json.dumps(campaign.character_picks)),
            )
            self._conn.commit()
        return campaign

    @staticmethod
    def _row_to_campaign(row) -> Campaign:
        try:
            setup_answers = json.loads(row[4]) if row[4] else {}
        except (json.JSONDecodeError, TypeError):
            setup_answers = {}
        generated_opening = row[5] if len(row) > 5 and row[5] is not None else ""
        combat_enabled = bool(row[6]) if len(row) > 6 and row[6] is not None else True
        try:
            character_picks = json.loads(row[7]) if len(row) > 7 and row[7] else {}
        except (json.JSONDecodeError, TypeError):
            character_picks = {}
        return Campaign(
            id=row[0], scenario_id=row[1], player_name=row[2],
            created_at=row[3], setup_answers=setup_answers,
            generated_opening=generated_opening,
            combat_enabled=combat_enabled,
            character_picks=character_picks,
        )

    def get_campaign(self, campaign_id: str) -> "Campaign | None":
        row = self._conn.execute(
            f"SELECT {self._CAMPAIGN_COLS} FROM campaigns WHERE id=?",
            (campaign_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_campaign(row)

    def get_campaigns(self, scenario_id: str) -> "list[Campaign]":
        rows = self._conn.execute(
            f"SELECT {self._CAMPAIGN_COLS} FROM campaigns "
            "WHERE scenario_id=? ORDER BY created_at DESC",
            (scenario_id,),
        ).fetchall()
        return [self._row_to_campaign(r) for r in rows]

    def update_setup_answers(self, campaign_id: str, answers: dict) -> bool:
        """Persist setup wizard answers for a campaign."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE campaigns SET setup_answers=? WHERE id=?",
                (json.dumps(answers), campaign_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def update_combat_enabled(self, campaign_id: str, enabled: bool) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE campaigns SET combat_enabled=? WHERE id=?",
                (1 if enabled else 0, campaign_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def update_generated_opening(self, campaign_id: str, text: str) -> bool:
        """Persist an AI-generated opening for the campaign."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE campaigns SET generated_opening=? WHERE id=?",
                (text or "", campaign_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_campaign(self, campaign_id: str) -> bool:
        """Delete a campaign by id. Returns True if a row was deleted."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM campaigns WHERE id=?", (campaign_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ── character pack ──────────────────────────────────────────────
    _CHARACTER_COLS = (
        "id, scenario_id, name, role, description, portrait_url, is_player, created_at"
    )

    def add_character(
        self,
        scenario_id: str,
        name: str,
        role: str = "",
        description: str = "",
        portrait_url: str = "",
        is_player: bool = False,
    ) -> ScenarioCharacter:
        character = ScenarioCharacter(
            id=str(uuid.uuid4()),
            scenario_id=scenario_id,
            name=name,
            role=role,
            description=description,
            portrait_url=portrait_url,
            is_player=is_player,
            created_at=datetime.utcnow().isoformat(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO scenario_characters "
                "(id, scenario_id, name, role, description, portrait_url, is_player, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (character.id, character.scenario_id, character.name, character.role,
                 character.description, character.portrait_url,
                 1 if character.is_player else 0, character.created_at),
            )
            self._conn.commit()
        return character

    @staticmethod
    def _row_to_look(row) -> CharacterLook:
        return CharacterLook(
            id=row[0], character_id=row[1], outfit=row[2], expression=row[3],
            image_url=row[4] or "", sort_order=row[5] or 0,
        )

    def list_looks(self, character_id: str) -> "list[CharacterLook]":
        rows = self._conn.execute(
            "SELECT id, character_id, outfit, expression, image_url, sort_order "
            "FROM character_looks WHERE character_id=? "
            "ORDER BY sort_order ASC, outfit ASC, expression ASC",
            (character_id,),
        ).fetchall()
        return [self._row_to_look(r) for r in rows]

    @staticmethod
    def _row_to_character(row, looks: "list[CharacterLook] | None" = None) -> ScenarioCharacter:
        return ScenarioCharacter(
            id=row[0], scenario_id=row[1], name=row[2], role=row[3] or "",
            description=row[4] or "", portrait_url=row[5] or "",
            is_player=bool(row[6]), created_at=row[7],
            looks=list(looks or []),
        )

    def get_character(self, character_id: str) -> "ScenarioCharacter | None":
        row = self._conn.execute(
            f"SELECT {self._CHARACTER_COLS} FROM scenario_characters WHERE id=?",
            (character_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_character(row, self.list_looks(character_id))

    def list_characters(self, scenario_id: str) -> "list[ScenarioCharacter]":
        rows = self._conn.execute(
            f"SELECT {self._CHARACTER_COLS} FROM scenario_characters "
            "WHERE scenario_id=? ORDER BY is_player DESC, created_at ASC",
            (scenario_id,),
        ).fetchall()
        return [self._row_to_character(r, self.list_looks(r[0])) for r in rows]

    def update_character(self, character_id: str, **fields) -> bool:
        allowed = ("name", "role", "description", "portrait_url", "is_player")
        sets, values = [], []
        for key in allowed:
            if key not in fields or fields[key] is None:
                continue
            value = fields[key]
            if key == "is_player":
                value = 1 if value else 0
            sets.append(f"{key}=?")
            values.append(value)
        if not sets:
            return False
        values.append(character_id)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE scenario_characters SET {', '.join(sets)} WHERE id=?", values
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_character(self, character_id: str) -> bool:
        with self._lock:
            self._conn.execute(
                "DELETE FROM character_looks WHERE character_id=?", (character_id,)
            )
            cursor = self._conn.execute(
                "DELETE FROM scenario_characters WHERE id=?", (character_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def add_look(
        self,
        character_id: str,
        outfit: str,
        expression: str,
        image_url: str = "",
        sort_order: int | None = None,
    ) -> CharacterLook:
        if sort_order is None:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM character_looks "
                "WHERE character_id=?",
                (character_id,),
            ).fetchone()
            sort_order = int(row[0] or 0)
        look = CharacterLook(
            id=str(uuid.uuid4()), character_id=character_id,
            outfit=outfit or "默认", expression=expression or "平静",
            image_url=image_url, sort_order=sort_order,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO character_looks "
                "(id, character_id, outfit, expression, image_url, sort_order) "
                "VALUES (?,?,?,?,?,?)",
                (look.id, look.character_id, look.outfit, look.expression,
                 look.image_url, look.sort_order),
            )
            self._conn.commit()
        return look

    def get_look(self, look_id: str) -> "CharacterLook | None":
        row = self._conn.execute(
            "SELECT id, character_id, outfit, expression, image_url, sort_order "
            "FROM character_looks WHERE id=?",
            (look_id,),
        ).fetchone()
        return self._row_to_look(row) if row else None

    def update_look(self, look_id: str, **fields) -> bool:
        allowed = ("outfit", "expression", "image_url", "sort_order")
        sets, values = [], []
        for key in allowed:
            if key in fields and fields[key] is not None:
                sets.append(f"{key}=?")
                values.append(fields[key])
        if not sets:
            return False
        values.append(look_id)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE character_looks SET {', '.join(sets)} WHERE id=?", values
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_look(self, look_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM character_looks WHERE id=?", (look_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def update_campaign_character(self, campaign_id: str, picks: dict) -> bool:
        """Persist the player's chosen character look for a campaign."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE campaigns SET character_json=? WHERE id=?",
                (json.dumps(picks or {}), campaign_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_scenario(self, scenario_id: str) -> bool:
        """Delete a scenario and all its story cards and campaigns."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM story_cards WHERE scenario_id=?", (scenario_id,)
            )
            self._conn.execute(
                "DELETE FROM character_looks WHERE character_id IN "
                "(SELECT id FROM scenario_characters WHERE scenario_id=?)",
                (scenario_id,),
            )
            self._conn.execute(
                "DELETE FROM scenario_characters WHERE scenario_id=?", (scenario_id,)
            )
            self._conn.execute(
                "DELETE FROM scenario_knowledge WHERE scenario_id=?", (scenario_id,)
            )
            self._conn.execute(
                "DELETE FROM scenario_play_config WHERE scenario_id=?", (scenario_id,)
            )
            self._conn.execute(
                "DELETE FROM campaigns WHERE scenario_id=?", (scenario_id,)
            )
            cursor = self._conn.execute(
                "DELETE FROM scenarios WHERE id=?", (scenario_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------- knowledge entries
    _KNOWLEDGE_COLS = (
        "id, scenario_id, layer, title, content, tags, depth, always_on, "
        "mode_scope, enabled, sort_order, audience_npc_ids, created_at"
    )

    @staticmethod
    def _row_to_knowledge(row) -> KnowledgeEntry:
        try:
            tags = json.loads(row[5]) if row[5] else []
            audience = json.loads(row[11]) if row[11] else []
        except (json.JSONDecodeError, TypeError):
            tags, audience = [], []
        return KnowledgeEntry(
            id=row[0],
            scenario_id=row[1],
            layer=row[2],
            title=row[3],
            content=row[4] or "",
            tags=tags,
            depth=row[6],
            always_on=bool(row[7]),
            mode_scope=row[8],
            enabled=bool(row[9]),
            sort_order=row[10],
            audience_npc_ids=audience,
            created_at=row[12],
        )

    def list_knowledge(
        self, scenario_id: str, include_disabled: bool = True
    ) -> "list[KnowledgeEntry]":
        sql = f"SELECT {self._KNOWLEDGE_COLS} FROM scenario_knowledge WHERE scenario_id=?"
        if not include_disabled:
            sql += " AND enabled=1"
        sql += " ORDER BY depth ASC, sort_order ASC, created_at ASC"
        rows = self._conn.execute(sql, (scenario_id,)).fetchall()
        return [self._row_to_knowledge(row) for row in rows]

    def get_knowledge(self, entry_id: str) -> "KnowledgeEntry | None":
        row = self._conn.execute(
            f"SELECT {self._KNOWLEDGE_COLS} FROM scenario_knowledge WHERE id=?",
            (entry_id,),
        ).fetchone()
        return self._row_to_knowledge(row) if row else None

    def add_knowledge(
        self, scenario_id: str, entry: KnowledgeEntryCreate
    ) -> KnowledgeEntry:
        record = KnowledgeEntry(
            **entry.model_dump(),
            id=str(uuid.uuid4()),
            scenario_id=scenario_id,
            created_at=datetime.utcnow().isoformat(),
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO scenario_knowledge ({self._KNOWLEDGE_COLS}) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.scenario_id,
                    record.layer,
                    record.title,
                    record.content,
                    json.dumps(record.tags),
                    record.depth,
                    1 if record.always_on else 0,
                    record.mode_scope,
                    1 if record.enabled else 0,
                    record.sort_order,
                    json.dumps(record.audience_npc_ids),
                    record.created_at,
                ),
            )
            self._conn.commit()
        return record

    def update_knowledge(
        self, entry_id: str, patch: KnowledgeEntryPatch
    ) -> "KnowledgeEntry | None":
        fields = patch.model_dump(exclude_none=True)
        if fields:
            sets: list = []
            values: list = []
            for key, value in fields.items():
                if key in ("tags", "audience_npc_ids"):
                    value = json.dumps(value)
                elif key in ("always_on", "enabled"):
                    value = 1 if value else 0
                sets.append(f"{key}=?")
                values.append(value)
            values.append(entry_id)
            with self._lock:
                self._conn.execute(
                    f"UPDATE scenario_knowledge SET {', '.join(sets)} WHERE id=?",
                    values,
                )
                self._conn.commit()
        return self.get_knowledge(entry_id)

    def delete_knowledge(self, entry_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM scenario_knowledge WHERE id=?", (entry_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    # ----------------------------------------------------------- play config
    def get_play_config(self, scenario_id: str) -> PlayConfig:
        row = self._conn.execute(
            "SELECT scenario_id, mode, storyline_json, max_depth, "
            "context_budget_chars, model_preset_id, intent_preset_id, narrator_prompt, "
            "dialogue_preset_id, summary_preset_id, updated_at "
            "FROM scenario_play_config WHERE scenario_id=?",
            (scenario_id,),
        ).fetchone()
        if not row:
            return PlayConfig(scenario_id=scenario_id)
        try:
            storyline = Storyline.model_validate_json(row[2] or "{}")
        except Exception:  # noqa: BLE001 - stored data predates validation
            storyline = Storyline()
        return PlayConfig(
            scenario_id=row[0],
            mode=row[1],
            storyline=storyline,
            max_depth=row[3] if row[3] is not None else 9,
            context_budget_chars=row[4] if row[4] is not None else 0,
            model_preset_id=row[5] or "",
            intent_preset_id=row[6] or "",
            narrator_prompt=row[7] or "",
            dialogue_preset_id=row[8] or "",
            summary_preset_id=row[9] or "",
            updated_at=row[10] or "",
        )

    def set_play_config(self, scenario_id: str, config: PlayConfig) -> PlayConfig:
        now = datetime.utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO scenario_play_config "
                "(scenario_id, mode, storyline_json, max_depth, "
                "context_budget_chars, model_preset_id, intent_preset_id, "
                "narrator_prompt, dialogue_preset_id, summary_preset_id, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scenario_id) DO UPDATE SET mode=excluded.mode, "
                "storyline_json=excluded.storyline_json, "
                "max_depth=excluded.max_depth, "
                "context_budget_chars=excluded.context_budget_chars, "
                "model_preset_id=excluded.model_preset_id, "
                "intent_preset_id=excluded.intent_preset_id, "
                "narrator_prompt=excluded.narrator_prompt, "
                "dialogue_preset_id=excluded.dialogue_preset_id, "
                "summary_preset_id=excluded.summary_preset_id, "
                "updated_at=excluded.updated_at",
                (
                    scenario_id,
                    config.mode,
                    config.storyline.model_dump_json(),
                    config.max_depth,
                    config.context_budget_chars,
                    config.model_preset_id,
                    config.intent_preset_id,
                    config.narrator_prompt,
                    config.dialogue_preset_id,
                    config.summary_preset_id,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_play_config(scenario_id)

    # --------------------------------------------------------- model presets
    _PRESET_COLS = (
        "id, name, role, provider, model, temperature, max_tokens, notes, "
        "is_builtin, created_at, extras_json"
    )
    _PRESET_EXTRA_FIELDS = (
        "top_p", "top_k", "min_p", "repetition_penalty",
        "presence_penalty", "frequency_penalty", "seed", "stop",
        "system_prompt", "context_window",
    )

    @classmethod
    def _row_to_preset(cls, row) -> ModelPreset:
        try:
            extras = json.loads(row[10]) if row[10] else {}
        except (json.JSONDecodeError, TypeError):
            extras = {}
        return ModelPreset(
            id=row[0],
            name=row[1],
            role=row[2],
            provider=row[3] or "",
            model=row[4] or "",
            temperature=row[5],
            max_tokens=row[6],
            notes=row[7] or "",
            is_builtin=bool(row[8]),
            created_at=row[9],
            **{
                key: extras[key]
                for key in cls._PRESET_EXTRA_FIELDS
                if key in extras
            },
        )

    def _seed_builtin_presets(self) -> None:
        builtins = [
            ("builtin-balanced", "温和叙事", "narrator", 0.9, 500,
             "默认叙事预设：温度略高，篇幅短，跟随全局模型路由。"),
            ("builtin-grim", "冷峻叙事", "narrator", 0.6, 420,
             "适合末世/悬疑：低温、克制、少形容词。"),
            ("builtin-fast", "极简叙事", "narrator", 0.4, 260,
             "低延迟：只写一到两句，适合连续探索。"),
            ("builtin-intent", "意图解析", "intent", 0.1, 300,
             "只用于把自然语言翻译成结构化行动，不产生叙事。"),
        ]
        with self._lock:
            for preset_id, name, role, temperature, max_tokens, notes in builtins:
                self._conn.execute(
                    "INSERT OR IGNORE INTO model_presets "
                    "(id, name, role, provider, model, temperature, max_tokens, "
                    "notes, is_builtin, created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
                    (
                        preset_id,
                        name,
                        role,
                        "",
                        "",
                        temperature,
                        max_tokens,
                        notes,
                        datetime.utcnow().isoformat(),
                    ),
                )
            self._conn.commit()

    def list_model_presets(self) -> "list[ModelPreset]":
        rows = self._conn.execute(
            f"SELECT {self._PRESET_COLS} FROM model_presets "
            "ORDER BY is_builtin DESC, created_at ASC"
        ).fetchall()
        return [self._row_to_preset(row) for row in rows]

    def get_model_preset(self, preset_id: str) -> "ModelPreset | None":
        row = self._conn.execute(
            f"SELECT {self._PRESET_COLS} FROM model_presets WHERE id=?",
            (preset_id,),
        ).fetchone()
        return self._row_to_preset(row) if row else None

    def add_model_preset(self, preset: ModelPresetCreate) -> ModelPreset:
        record = ModelPreset(
            **preset.model_dump(),
            id=str(uuid.uuid4()),
            is_builtin=False,
            created_at=datetime.utcnow().isoformat(),
        )
        with self._lock:
            self._conn.execute(
                f"INSERT INTO model_presets ({self._PRESET_COLS}) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.name,
                    record.role,
                    record.provider,
                    record.model,
                    record.temperature,
                    record.max_tokens,
                    record.notes,
                    0,
                    record.created_at,
                    json.dumps(
                        {
                            key: getattr(record, key)
                            for key in self._PRESET_EXTRA_FIELDS
                        }
                    ),
                ),
            )
            self._conn.commit()
        return record

    def update_model_preset(
        self, preset_id: str, patch: ModelPresetPatch
    ) -> "ModelPreset | None":
        fields = patch.model_dump(exclude_none=True)
        extras = {
            key: fields.pop(key)
            for key in list(fields)
            if key in self._PRESET_EXTRA_FIELDS
        }
        if fields or extras:
            sets = [f"{key}=?" for key in fields]
            values = list(fields.values())
            if extras:
                current = self.get_model_preset(preset_id)
                merged = (
                    {key: getattr(current, key) for key in self._PRESET_EXTRA_FIELDS}
                    if current is not None
                    else {}
                )
                merged.update(extras)
                sets.append("extras_json=?")
                values.append(json.dumps(merged))
            values.append(preset_id)
            with self._lock:
                self._conn.execute(
                    f"UPDATE model_presets SET {', '.join(sets)} WHERE id=?", values
                )
                self._conn.commit()
        return self.get_model_preset(preset_id)

    def delete_model_preset(self, preset_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM model_presets WHERE id=? AND is_builtin=0",
                (preset_id,),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
