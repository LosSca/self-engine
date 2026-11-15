from __future__ import annotations
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace

from app.db.event_store import EventStore, Event, EventType
from app.engines.memory_consolidation import (
    consolidation_mode,
    select_merge_batch,
    theme_keywords,
)
from app.models.memory import EvidenceType, evidence_type_of_event, trust_of
from app.services.memory_perspective import (
    BELIEVES,
    Claim,
    NpcPerspective,
    build_perspective,
    facts_for_prompt,
    filter_facts_for_observer,
)
from app.services.retrieval.memory_retrieval import (
    build_crystal_vector_channel,
    build_entity_index_from_store,
    crystal_docs_from_engine,
    fact_docs_from_store,
    hybrid_rank,
)
from app.services.retrieval.query_builder import build_graph_query, build_memory_query
from app.services.retrieval.tokenizer import tokenize_text
from app.utils.json_parsing import parse_json_dict

logger = logging.getLogger(__name__)


# ── Stop words for keyword extraction (EN + PT) ─────────────────────
# Mirrors the set used in game_session._extract_context_keywords so that
# crystal scoring stays consistent with story-card scoring.
_STOP_WORDS: set[str] = {
    "the", "and", "for", "that", "this", "with", "you", "your", "are",
    "was", "were", "has", "have", "had", "been", "will", "not", "but",
    "from", "they", "she", "his", "her", "its", "our", "que", "para",
    "com", "uma", "por", "ele", "ela", "seu", "sua", "dos", "das",
    "nos", "nas", "mais", "como", "não", "está", "isso", "esse",
    "essa", "são", "tem", "foi", "ser", "ter", "mas", "quando",
    "sobre", "entre", "depois", "antes", "muito", "pode", "seus",
    "suas", "ainda", "também", "apenas", "cada", "outro", "outra",
}


def _extract_keywords(text: str) -> set[str]:
    """Query keywords, in whatever scripts the text uses.

    The old version matched `[a-zA-ZÀ-ÿ]{3,}` only, so a Chinese query produced
    an empty set and every crystal scored identically - semantic recall silently
    did nothing for the language the game is written in. The shared tokenizer
    emits Latin words and CJK bigrams, so a Chinese scene now has keywords.
    """
    if not text:
        return set()
    return {token for token in tokenize_text(text) if token not in _STOP_WORDS}


def _rag_crystals_enabled() -> bool:
    """Feature flag — defaults ON. Set LUNAR_FEATURE_RAG_CRYSTALS=0/false to disable."""
    raw = os.environ.get("LUNAR_FEATURE_RAG_CRYSTALS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _retrieval_mode() -> str:
    """How crystal recall is ranked: "hybrid" ranking (default) or "score".

    MEMORY_RETRIEVAL=score keeps the pre-P2 behaviour - rank by _score_crystal
    alone - so the evaluation harness can measure the hybrid channels against
    it on the same corpus. The hybrid path still uses _score_crystal, as one of
    its channels, so the old ranking is never thrown away.
    """
    raw = os.environ.get("MEMORY_RETRIEVAL", "hybrid").strip().lower()
    return "score" if raw in ("score", "legacy", "old", "0", "false", "off") else "hybrid"


def _scoring_mode() -> str:
    """Which ranking formula to use: "hybrid" (default) or "legacy".

    Kept as a runtime switch rather than a rewrite so the evaluation harness
    can rank the same corpus twice and compare, and so a save that suddenly
    recalls differently can be rolled back by setting one variable.
    """
    raw = os.environ.get("MEMORY_SCORING", "hybrid").strip().lower()
    return "legacy" if raw in ("legacy", "old", "0", "false", "off") else "hybrid"


def estimate_tokens_for_crystal(crystal: "MemoryCrystal") -> int:
    """Rough token estimate for a crystal's ai_content payload (~4 chars/token)."""
    text = crystal.ai_content or ""
    return max(1, len(text) // 4)


class CrystalTier(str, Enum):
    SHORT = "SHORT"     # ~4 actions → ultra-compressed recap
    MEDIUM = "MEDIUM"   # 4 SHORTs → consolidated summary
    LONG = "LONG"       # 4 MEDIUMs → high-level arc summary
    MEMORY = "MEMORY"   # 4 LONGs → permanent world facts

    @property
    def consolidation_count(self) -> int:
        """How many of the previous tier are needed to trigger this tier."""
        return 4

    @property
    def previous_tier(self) -> CrystalTier | None:
        _prev = {
            CrystalTier.MEDIUM: CrystalTier.SHORT,
            CrystalTier.LONG: CrystalTier.MEDIUM,
            CrystalTier.MEMORY: CrystalTier.LONG,
        }
        return _prev.get(self)

    @property
    def next_tier(self) -> CrystalTier | None:
        _next = {
            CrystalTier.SHORT: CrystalTier.MEDIUM,
            CrystalTier.MEDIUM: CrystalTier.LONG,
            CrystalTier.LONG: CrystalTier.MEMORY,
        }
        return _next.get(self)


@dataclass
class MemoryCrystal:
    campaign_id: str
    tier: CrystalTier
    content: str           # Player-facing executive summary
    ai_content: str        # Ultra-compressed memory for LLM context
    event_count: int
    consumed: bool = False  # True when consolidated into a higher tier
    source_start_created_at: str | None = None
    source_end_created_at: str | None = None
    # Every source must be witnessed; an empty audience is narrator/player-only.
    witnessed_by: list[str] = field(default_factory=list)
    audience_resolved: bool = field(default=False, repr=False, compare=False)
    # ── provenance (why this memory exists and how far to trust it) ──
    #: Stable id derived from the sources, so a rebuilt crystal keeps its id.
    memory_id: str = ""
    #: Where the content came from: "events" (crystallised), "consolidation"
    #: (merged from lower tiers) or "" for crystals that predate this field.
    source: str = ""
    #: Who the memory belongs to: "player" for the shared record, or an NPC
    #: name for a record only that character witnessed.
    observer: str = "player"
    #: 0..1 confidence in the content. Consolidated tiers are less certain
    #: about wording, never about whose memory it is.
    confidence: float = 1.0
    #: 0..1 salience, from the scorer's rank when available.
    importance: float = 0.0
    #: "player" (shared) or "npc" (witnessed-only).
    visibility: str = "player"
    #: Proper names the content mentions, for scene-scoped retrieval.
    related_entities: list[str] = field(default_factory=list)
    #: "candidate" until the tier is settled, "committed" afterwards.
    status: str = "committed"
    #: What kind of authority this memory carries (see
    #: app.models.memory.EvidenceType). A crystal built only from committed
    #: actions is a record of what happened; one built from narration is prose
    #: *about* a record, and the weakest source governs the whole.
    evidence_type: str = ""
    #: Callers that must never lose this memory (authored canon, a pinned
    #: commitment) set this; retrieval keeps it before scoring.
    always_include: bool = False
    #: Witness names as written, before case folding. `witnessed_by` is
    #: normalised for comparison, so the display label needs its own copy.
    witnessed_names: list[str] = field(default_factory=list, repr=False, compare=False)
    #: The plot thread this crystal belongs to (an entity or place name), or ""
    #: for a crystal that predates theme-driven consolidation.
    theme_key: str = ""
    #: Words shared by the crystals a merge replaced. Kept because a paraphrase
    #: can drop a proper name the original prose carried.
    theme_keywords: list[str] = field(default_factory=list)
    #: memory_ids of the crystals this one replaced. A merge is always traceable
    #: back to the summaries it paraphrases, and from those to raw events.
    source_memory_ids: list[str] = field(default_factory=list)

    def provenance_label(self) -> str:
        """Short "(source; whose; trust)" label, or "" for legacy crystals."""
        if not self.source and not self.witnessed_by:
            return ""
        if self.witnessed_by:
            names = list(self.witnessed_names or self.witnessed_by)[:3]
            whose = "witnessed by " + ", ".join(names)
        elif self.observer == "player":
            whose = "player account"
        else:
            whose = f"observed by {self.observer}"
        if self.confidence < 0.5:
            trust = "uncertain - a lead, not a fact"
        elif self.confidence < 0.85:
            trust = "paraphrased"
        else:
            trust = "verbatim record"
        return f"({self.source or 'record'}; {whose}; {trust})"

    @property
    def memory_type(self) -> str:
        """The five-way classification this tier maps onto.

        SHORT/MEDIUM/LONG are all episodic - they differ in compression, not in
        kind. MEMORY is the permanent knowledge tier, which is semantic.
        """
        name = self.tier.value if isinstance(self.tier, CrystalTier) else str(self.tier)
        return "semantic" if name == CrystalTier.MEMORY.value else "episodic"


def _normalized_witnesses(names) -> set[str]:
    if not isinstance(names, (list, tuple, set)):
        return set()
    return {name.strip().casefold() for name in names if isinstance(name, str) and name.strip()}


def _common_witnesses(sources) -> list[str]:
    audiences = [_normalized_witnesses(source.witnessed_by) for source in sources]
    return sorted(set.intersection(*audiences)) if audiences else []


def _display_witnesses(sources, normalized: list[str]) -> list[str]:
    """Spell the shared audience the way its witnesses were first written."""
    wanted = set(normalized)
    names: list[str] = []
    for source in sources or ():
        for name in getattr(source, "witnessed_by", None) or ():
            if not isinstance(name, str):
                continue
            cleaned = name.strip()
            if cleaned and cleaned.casefold() in wanted and cleaned not in names:
                names.append(cleaned)
    return names


def _matched_witnesses(sources) -> list[str]:
    """Witnesses as written, when every source agrees on at least one."""
    shared = _common_witnesses(sources)
    return _display_witnesses(sources, shared) if shared else []


#: Confidence per tier. Consolidation paraphrases more aggressively the
#: higher it goes, so a merged memory is worth less as literal wording —
#: never less as a record of who witnessed it.
TIER_CONFIDENCE = {
    "SHORT": 1.0,
    "MEDIUM": 0.9,
    "LONG": 0.8,
    "MEMORY": 0.7,
}


def derive_memory_id(campaign_id: str, tier: str, source_start: str | None, source_end: str | None) -> str:
    """Idempotent crystal id: the same span in the same campaign is one memory."""
    raw = "|".join([campaign_id or "", tier or "", source_start or "", source_end or ""])
    return "mem_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _related_entities(sources) -> list[str]:
    """Proper names the source events mention, most frequent first."""
    counts: dict[str, int] = {}
    for source in sources or ():
        for name in getattr(source, "entities", None) or ():
            if not isinstance(name, str) or not name.strip():
                continue
            counts[name.strip()] = counts.get(name.strip(), 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    return [name for name, _ in ordered[:12]]


def _evidence_type_for_sources(sources) -> str:
    """The evidence kind a memory carries, as its weakest source.

    A crystal is prose written by a model, but the *claim* it records came from
    specific events. If every source is a settled event the memory speaks with
    the authority of the engine; one narration source is enough to demote the
    whole memory to prose, because a reader cannot tell which clause came from
    where. Taking the minimum matches how the rest of this module treats
    provenance: a merge inherits the lowest confidence, and audiences intersect
    rather than union.
    """
    weakest = EvidenceType.ENGINE_STATE.value
    weakest_trust = 2.0
    seen = False
    for source in sources or ():
        raw = getattr(source, "evidence_type", "") or ""
        if raw:
            kind = str(raw)
        elif hasattr(source, "event_type"):
            kind = evidence_type_of_event(getattr(source, "event_type")).value
        elif getattr(source, "tier", None) is not None:
            # A lower-tier crystal that predates this field: it was written by
            # a model from a model's paraphrase, so treat it as prose.
            kind = EvidenceType.LLM_NARRATIVE.value
        else:
            continue
        seen = True
        trust = trust_of(kind)
        if trust < weakest_trust:
            weakest, weakest_trust = kind, trust
    return weakest if seen else EvidenceType.LLM_NARRATIVE.value


def stamp_provenance(crystal: MemoryCrystal, *, sources=(), source: str = "events", importance: float = 0.0) -> MemoryCrystal:
    """Fill the provenance fields a caller knows, leaving the rest intact."""
    tier_name = crystal.tier.value if isinstance(crystal.tier, CrystalTier) else str(crystal.tier)
    crystal.memory_id = crystal.memory_id or derive_memory_id(
        crystal.campaign_id, tier_name, crystal.source_start_created_at, crystal.source_end_created_at,
    )
    crystal.source = crystal.source or source
    crystal.observer = crystal.observer or "player"
    crystal.visibility = "npc" if crystal.witnessed_by else "player"
    if not crystal.witnessed_names:
        crystal.witnessed_names = _display_witnesses(sources, crystal.witnessed_by or [])
    crystal.confidence = TIER_CONFIDENCE.get(tier_name, 1.0)
    if importance:
        crystal.importance = round(float(importance), 4)
    if not crystal.related_entities:
        crystal.related_entities = _related_entities(sources)
    crystal.status = "committed"
    if not crystal.evidence_type:
        crystal.evidence_type = _evidence_type_for_sources(sources)
    return crystal


# ── Compression prompts per tier ────────────────────────────────────
# Each tier consolidates the previous tier into a richer structured-JSON
# memory entry. The format is fact-preserving — proper names, physical
# descriptions, mission details, dialogue intent are kept verbatim. The
# pyramid (SHORT→MEDIUM→LONG→MEMORY) widens scope, not lossiness.

_CRYSTAL_SCHEMA_DOC = (
    "Return ONLY valid JSON (no markdown fences) with this exact schema:\n"
    "{\n"
    '  "ai": {\n'
    '    "events": [\n'
    '      {"who": "<actor names>", "action": "<what they did>", "where": "<location>", "result": "<outcome / state change>"}\n'
    '    ],\n'
    '    "characters": {\n'
    '      "<Character Name>": {\n'
    '        "description": "<physical traits, age, distinguishing features — verbatim from source>",\n'
    '        "state": "<current condition / what just happened to them>",\n'
    '        "relationship_to_player": "<ally|enemy|neutral|hired-by|hired-player|family|...>",\n'
    '        "knows_player_as": "<name/identity the NPC associates with the player>"\n'
    '      }\n'
    '    },\n'
    '    "items": [\n'
    '      {"name": "<item name>", "owner": "<current owner>", "status": "acquired|used|lost|given|destroyed"}\n'
    '    ],\n'
    '    "promises_or_missions": [\n'
    '      "<verbatim text of any open commitment, deal, quest, or threat — include who promised what to whom>"\n'
    '    ],\n'
    '    "world_facts": [\n'
    '      "<lasting facts revealed this scene: location names, faction info, lore, rules>"\n'
    '    ]\n'
    '  },\n'
    '  "summary": "<short human-readable text for the player UI>"\n'
    "}\n"
)

_CRYSTAL_INTEGRITY_RULES = (
    "INTEGRITY RULES (violations are failures):\n"
    "- Preserve proper names EXACTLY as in source. Do not normalize, translate, or abbreviate.\n"
    "- NEVER substitute a character's name with a similar-sounding canonical name from "
    "popular fiction. If the source says 'Lena', the output says 'Lena' — never 'Nami', "
    "'Lana', etc. The same applies to every other name.\n"
    "- Preserve physical descriptions exactly: hair color, eye color, age, height, scars, "
    "clothing, voice traits. These details are critical for narrative continuity.\n"
    "- Preserve mission, quest, and promise details exactly: who hired whom, the target, "
    "the conditions, the payment, the deadline. Open promises must survive consolidation.\n"
    "- When a character introduces themselves or reveals identity, keep the verbatim phrasing.\n"
    "- Do NOT invent details that are not in the source.\n"
    "- Do NOT abbreviate words, drop vowels, or use telegraph-style symbols. Write full words.\n"
    "- Do NOT remove information just because it seems descriptive — descriptions are facts.\n"
)

_CRYSTAL_PROMPTS = {
    CrystalTier.SHORT: (
        "You are a structured memory recorder for an RPG AI engine.\n"
        "Record the recent events into a fact-preserving JSON memory entry.\n\n"
        + _CRYSTAL_SCHEMA_DOC + "\n"
        + _CRYSTAL_INTEGRITY_RULES + "\n"
        "SCOPE for SHORT tier:\n"
        "- Covers ~4 recent actions in a single scene.\n"
        "- Include every named character that appears, even briefly.\n"
        "- Include physical descriptions the first time they are mentioned, and keep them "
        "if mentioned again with new detail.\n"
        "- Include short verbatim quotes when they reveal intent, identity, or plot info.\n"
        "- 'summary': 1-2 short sentences in normal language for the player UI."
    ),
    CrystalTier.MEDIUM: (
        "You are a structured memory consolidator for an RPG AI engine.\n"
        "Merge several SHORT crystal JSON entries into a single MEDIUM-tier consolidated entry.\n"
        "Same schema. Wider scope (multiple scenes / a sub-arc).\n\n"
        + _CRYSTAL_SCHEMA_DOC + "\n"
        + _CRYSTAL_INTEGRITY_RULES + "\n"
        "CONSOLIDATION RULES for MEDIUM tier:\n"
        "- Preserve causal order of events. Group closely-related events into one richer "
        "entry only if they share who/where/result.\n"
        "- Merge per-character entries: if the same character appears across multiple SHORTs, "
        "produce a single entry that keeps every distinct descriptor and the latest known state.\n"
        "- Drop only true duplicates. Do not drop a fact just because it seems minor.\n"
        "- Keep every open promise/mission until it is explicitly resolved.\n"
        "- 'summary': 2-3 sentences in normal language for the player UI."
    ),
    CrystalTier.LONG: (
        "You are a structured memory consolidator for an RPG AI engine.\n"
        "Merge MEDIUM crystal JSON entries into a single LONG-tier story-arc entry.\n"
        "Same schema. Covers a major story arc (dozens of actions).\n\n"
        + _CRYSTAL_SCHEMA_DOC + "\n"
        + _CRYSTAL_INTEGRITY_RULES + "\n"
        "CONSOLIDATION RULES for LONG tier:\n"
        "- Group events into the major beats of the arc, but keep enough specificity that "
        "the narrator can reconstruct what happened.\n"
        "- Keep every named character that appeared, with their key descriptors and final "
        "state at the end of the arc.\n"
        "- Keep every open promise/mission. Mark resolved ones with result in 'state'.\n"
        "- 'summary': 3-4 sentences in normal language for the player UI."
    ),
    CrystalTier.MEMORY: (
        "You are a structured memory consolidator for an RPG AI engine.\n"
        "Merge LONG crystal JSON entries into a single PERMANENT-tier world-facts entry.\n"
        "Same schema. Covers the player's permanent identity, completed arcs, lasting world state.\n\n"
        + _CRYSTAL_SCHEMA_DOC + "\n"
        + _CRYSTAL_INTEGRITY_RULES + "\n"
        "CONSOLIDATION RULES for MEMORY tier:\n"
        "- Keep the player's permanent identity, origin, core abilities, and reputation.\n"
        "- Keep every named character ever met, with their description and final state.\n"
        "- Keep all completed major arcs as one event entry each, with the resolution.\n"
        "- Keep all lasting allies, enemies, and faction standings.\n"
        "- 'summary': 4-5 sentences in normal language for the player UI — full story recap."
    ),
}


class MemoryEngine:
    AUTO_CRYSTALLIZE_THRESHOLD = 4  # events before creating a SHORT crystal
    MAX_CRYSTALLIZE_EVENTS = 200
    NPC_RAW_EVENT_LIMIT = 20
    AUDIENCE_POLICY = "source-intersection-v1"
    CONSOLIDATION_COUNT = 4  # N crystals of tier X → 1 crystal of tier X+1
    CRYSTALLIZE_EVENT_TYPES = (
        EventType.PLAYER_ACTION,
        EventType.NARRATOR_RESPONSE,
        EventType.WORLD_TICK,
        EventType.COMBAT_RESULT,
        EventType.TIMESKIP,
    )

    def __init__(
        self,
        event_store: EventStore,
        llm,
        graphiti_engine=None,
        fact_store=None,
        minds=None,
    ):
        self._store = event_store
        #: Optional NPC mind store (app.engines.npc_mind_engine). Only used to
        #: read back beliefs a character was told; memory works without it.
        self._minds = minds
        self._llm = llm
        self._graphiti = graphiti_engine
        #: Optional structured-fact layer (app.db.memory_fact_store). It is a
        #: derived index over the events this engine already keeps, so the
        #: engine works identically without it: facts only ever add channels.
        self._facts = fact_store
        self._crystals: dict[str, list[MemoryCrystal]] = {}
        self._crystallizing: set[str] = set()
        self._last_crystal_cursor: dict[str, str] = {}
        #: Last hybrid ranking, kept for the debug panel and for tests that
        #: assert *why* a crystal was recalled.
        self._last_retrieval: dict[str, dict] = {}
        self._vector_cache: dict[str, object] = {}

    def set_mind_store(self, minds) -> None:
        """Attach the NPC mind store so characters can be told things."""
        self._minds = minds

    @property
    def mind_store(self):
        return self._minds

    def set_fact_store(self, fact_store) -> None:
        self._facts = fact_store

    @property
    def fact_store(self):
        return self._facts

    def set_graphiti(self, graphiti_engine) -> None:
        if self._graphiti is None:
            self._graphiti = graphiti_engine

    def get_raw_context(self, campaign_id: str, limit: int = 10) -> list[Event]:
        return self._store.get_recent(campaign_id, limit=limit)

    # ── Crystal accessors ───────────────────────────────────────────

    def get_crystals(self, campaign_id: str) -> list[MemoryCrystal]:
        return self._crystals.get(campaign_id, [])

    def render_permanent_context(self, campaign_id: str) -> str:
        """Render only the MEMORY-tier crystals (permanent canon) as a cacheable block.

        Used by the FASE 2 zone split: this immutable, append-only canon lives in the
        cached zone, separate from the volatile recent-tier / DELTA context.
        """
        memory_crystals = [
            c for c in self._crystals.get(campaign_id, [])
            if c.tier == CrystalTier.MEMORY
        ]
        if not memory_crystals:
            return ""
        parts = ["=== PRMNT_MEM ==="]
        for c in memory_crystals:
            parts.append(self._with_provenance(c))
        return "\n".join(parts)

    @staticmethod
    def _with_provenance(crystal: MemoryCrystal) -> str:
        """Prefix crystal text with one line of provenance.

        Provenance travels with the text so the narrator can distinguish a
        verbatim record from a twice-consolidated paraphrase. Crystals written
        before provenance existed render exactly as before.
        """
        label = crystal.provenance_label()
        return f"{label}\n{crystal.ai_content}" if label else crystal.ai_content

    def _unconsumed_crystals(self, campaign_id: str, tier: CrystalTier) -> list[MemoryCrystal]:
        """Return crystals of a given tier that haven't been consolidated yet."""
        return [
            c for c in self._crystals.get(campaign_id, [])
            if c.tier == tier and not c.consumed
        ]

    def _latest_crystal(self, campaign_id: str, tier: CrystalTier) -> MemoryCrystal | None:
        for crystal in reversed(self._crystals.get(campaign_id, [])):
            if crystal.tier == tier:
                return crystal
        return None

    # ── SHORT crystal creation (from raw events) ────────────────────

    async def crystallize_short(self, campaign_id: str, language: str = "en") -> MemoryCrystal | None:
        """Create a SHORT crystal from recent uncrystallized events."""
        events = self._get_uncrystallized_events(
            campaign_id, limit=self.MAX_CRYSTALLIZE_EVENTS,
        )
        if not events:
            return None

        events_text = self._format_event_batch(events)
        ai_content, player_summary = await self._compress_with_llm(
            tier=CrystalTier.SHORT,
            source_text=events_text,
            max_tokens=2048,
            language=language,
        )
        if not ai_content:
            ai_content = self._fallback_short(events)
        if not player_summary:
            player_summary = self._fallback_player_summary(events)

        witnessed_by = _common_witnesses(events)
        crystal = MemoryCrystal(
            campaign_id=campaign_id,
            tier=CrystalTier.SHORT,
            content=player_summary,
            ai_content=ai_content,
            event_count=len(events),
            source_start_created_at=events[0].created_at,
            source_end_created_at=events[-1].created_at,
            witnessed_by=witnessed_by,
            audience_resolved=True,
        )
        stamp_provenance(crystal, sources=events, source="events")
        self._crystals.setdefault(campaign_id, []).append(crystal)
        self._persist_crystal(campaign_id, crystal)
        self._last_crystal_cursor[campaign_id] = events[-1].created_at
        logger.info(
            "SHORT crystal created: %d events → %d chars ai_content "
            "(campaign %s, witnesses=%s)",
            len(events), len(ai_content), campaign_id, witnessed_by,
        )
        return crystal

    # ── Tier consolidation (SHORT→MEDIUM→LONG→MEMORY) ──────────────

    async def _consolidate_tier(
        self, campaign_id: str, target_tier: CrystalTier, language: str = "en",
    ) -> MemoryCrystal | None:
        """Consolidate N crystals of the previous tier into 1 of target_tier."""
        prev_tier = target_tier.previous_tier
        if prev_tier is None:
            return None

        unconsumed = self._unconsumed_crystals(campaign_id, prev_tier)
        if len(unconsumed) < self.CONSOLIDATION_COUNT:
            return None

        # What belongs together is decided by thread, not by age: crystals that
        # share an entity (one plot line) merge with each other, and a crystal
        # the engine pinned as pivotal is never merged at all. In time mode
        # (MEMORY_CONSOLIDATION=time) this returns the historical oldest-N batch.
        to_merge = select_merge_batch(
            unconsumed, count=self.CONSOLIDATION_COUNT, mode=consolidation_mode(),
        )
        if len(to_merge) < 2:
            return None
        source_text = "\n---\n".join(c.ai_content for c in to_merge)
        merged_keywords = theme_keywords(to_merge)

        max_tokens = {
            CrystalTier.MEDIUM: 4096,
            CrystalTier.LONG: 8192,
            CrystalTier.MEMORY: 12288,
        }.get(target_tier, 4096)

        ai_content, player_summary = await self._compress_with_llm(
            tier=target_tier,
            source_text=source_text,
            max_tokens=max_tokens,
            language=language,
        )
        if not ai_content:
            # Fallback when LLM consolidation fails: keep all source ai_content
            # blobs verbatim as a JSON array so downstream consumers can still
            # parse / read each one. Lossless beats lossy here.
            ai_content = json.dumps(
                [c.ai_content for c in to_merge], ensure_ascii=False,
            )
        if not player_summary:
            player_summary = " ".join(c.content for c in to_merge)

        total_events = sum(c.event_count for c in to_merge)
        self._resolve_crystal_audiences(campaign_id)

        crystal = MemoryCrystal(
            campaign_id=campaign_id,
            tier=target_tier,
            content=player_summary,
            ai_content=ai_content,
            event_count=total_events,
            source_start_created_at=to_merge[0].source_start_created_at,
            source_end_created_at=to_merge[-1].source_end_created_at,
            witnessed_by=_common_witnesses(to_merge),
            audience_resolved=True,
        )
        stamp_provenance(
            crystal,
            sources=to_merge,
            source="consolidation",
            importance=max((c.importance for c in to_merge), default=0.0),
        )
        # A merge inherits its sources' provenance: the same observers, the
        # lowest confidence among them, and the union of mentioned names.
        crystal.witnessed_names = _display_witnesses(
            [SimpleNamespace(witnessed_by=c.witnessed_names or c.witnessed_by) for c in to_merge],
            crystal.witnessed_by or [],
        )
        crystal.observer = to_merge[0].observer or "player"
        # A merge is never as trustworthy as its own tier default: it is a
        # paraphrase of paraphrases, so step down one notch as well.
        inherited_confidence = min(
            [TIER_CONFIDENCE.get(crystal.tier.value, 1.0)] + [c.confidence for c in to_merge if c.confidence]
        )
        crystal.confidence = round(max(0.0, inherited_confidence - 0.02), 4)
        inherited = []
        for source_crystal in to_merge:
            for name in source_crystal.related_entities:
                if name not in inherited:
                    inherited.append(name)
        crystal.related_entities = inherited[:12]
        # The thread a merge came from, so a later retrieval can tell "this is
        # the Eileen arc" from "these four unrelated scenes". Keywords are
        # recorded alongside because the merged prose is what gets retrieved,
        # and a short tag list survives even when the model's paraphrase drops
        # a name.
        crystal.theme_key = (
            to_merge[0].related_entities[0] if to_merge and to_merge[0].related_entities else ""
        )
        crystal.theme_keywords = merged_keywords[:12]
        crystal.source_memory_ids = [
            (c.memory_id or "") for c in to_merge if getattr(c, "memory_id", "")
        ]
        self._crystals.setdefault(campaign_id, []).append(crystal)
        self._persist_crystal(campaign_id, crystal)

        # Mark source crystals as consumed
        for c in to_merge:
            c.consumed = True

        logger.info(
            "%s crystal created: %d %s crystals → %d chars ai_content (%d total events, campaign %s)",
            target_tier.value, len(to_merge), prev_tier.value,
            len(ai_content), total_events, campaign_id,
        )
        return crystal

    async def cascade_consolidation(self, campaign_id: str, language: str = "en") -> list[MemoryCrystal]:
        """Run the full consolidation cascade: SHORT→MEDIUM→LONG→MEMORY.

        After creating a SHORT crystal, checks if enough unconsumed crystals
        exist at each tier to trigger the next consolidation level.
        Returns all newly created crystals.
        """
        created: list[MemoryCrystal] = []
        for target_tier in (CrystalTier.MEDIUM, CrystalTier.LONG, CrystalTier.MEMORY):
            crystal = await self._consolidate_tier(campaign_id, target_tier, language=language)
            if crystal:
                created.append(crystal)
            else:
                break  # No point checking higher tiers if this one didn't trigger
        return created

    # ── Auto-crystallize (called from game_session) ─────────────────

    async def auto_crystallize_if_needed(self, campaign_id: str, language: str = "en") -> MemoryCrystal | None:
        """Auto-crystallize when raw events exceed threshold, then cascade."""
        if campaign_id in self._crystallizing:
            return None

        pending_events = self._get_uncrystallized_events(
            campaign_id, limit=self.AUTO_CRYSTALLIZE_THRESHOLD + 1,
        )
        if len(pending_events) < self.AUTO_CRYSTALLIZE_THRESHOLD:
            return None

        self._crystallizing.add(campaign_id)
        try:
            logger.info(
                "Auto-crystallizing %d events for campaign %s",
                len(pending_events), campaign_id,
            )
            short_crystal = await self.crystallize_short(campaign_id, language=language)
            if short_crystal:
                # Cascade: check if we can consolidate higher tiers
                cascaded = await self.cascade_consolidation(campaign_id, language=language)
                if cascaded:
                    logger.info(
                        "Cascade created %d higher-tier crystals: %s",
                        len(cascaded),
                        [c.tier.value for c in cascaded],
                    )
            return short_crystal
        except Exception:
            logger.warning("Auto-crystallization failed for campaign %s", campaign_id, exc_info=True)
            return None
        finally:
            self._crystallizing.discard(campaign_id)

    # ── Backward compat: crystallize() delegates to crystallize_short ──

    async def crystallize(
        self,
        campaign_id: str,
        tier: CrystalTier = CrystalTier.SHORT,
        force: bool = False,
        language: str = "en",
    ) -> MemoryCrystal:
        """Backward-compatible crystallize. SHORT → crystallize_short, others → consolidate."""
        if tier == CrystalTier.SHORT:
            result = await self.crystallize_short(campaign_id, language=language)
            if result:
                return result
            # Return latest if nothing new
            latest = self._latest_crystal(campaign_id, CrystalTier.SHORT)
            if latest:
                return latest
            return MemoryCrystal(
                campaign_id=campaign_id, tier=tier,
                content="No events yet.", ai_content="MEM:EMPTY", event_count=0,
            )
        else:
            result = await self._consolidate_tier(campaign_id, tier)
            if result:
                return result
            latest = self._latest_crystal(campaign_id, tier)
            if latest:
                return latest
            return MemoryCrystal(
                campaign_id=campaign_id, tier=tier,
                content="No events yet.", ai_content="MEM:EMPTY", event_count=0,
            )

    # ── LLM compression ────────────────────────────────────────────

    # Soft character limits per tier for ai_content. With structured-JSON
    # crystals on a 1M-context provider these are headroom guardrails, not
    # hard cuts — exceeding them logs a warning but the full JSON is kept,
    # because mid-cutting JSON corrupts the parse downstream.
    _AI_CHAR_LIMITS = {
        CrystalTier.SHORT: 2_000,
        CrystalTier.MEDIUM: 5_000,
        CrystalTier.LONG: 10_000,
        CrystalTier.MEMORY: 20_000,
    }

    async def _compress_with_llm(
        self, tier: CrystalTier, source_text: str, max_tokens: int = 1024,
        language: str = "en",
    ) -> tuple[str, str]:
        """Compress source text using the tier-specific prompt. Returns (ai_content, summary).

        ai_content is a JSON string (the serialized 'ai' object from the LLM response),
        kept verbatim so that downstream consumers (build_context_window, NPC mind,
        consolidation prompts) can re-parse the structured facts.
        """
        prompt_text = _CRYSTAL_PROMPTS.get(tier, _CRYSTAL_PROMPTS[CrystalTier.SHORT])
        if language and language != "en":
            prompt_text += (
                f"\n\nLANGUAGE: Write all string values (descriptions, states, summaries, "
                f"promises, world_facts, summary) in {language}. Proper names stay exactly "
                f"as in the source."
            )
        messages = [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": source_text},
        ]
        try:
            raw = await self._llm.complete(messages=messages, max_tokens=max_tokens, reasoning=False)
            parsed = parse_json_dict(raw)
            if parsed:
                ai_value = parsed.get("ai", parsed.get("ai_memory", ""))
                if isinstance(ai_value, (dict, list)):
                    ai = json.dumps(ai_value, ensure_ascii=False)
                else:
                    ai = str(ai_value).strip()
                summary = str(parsed.get("summary", parsed.get("player_summary", ""))).strip()
                char_limit = self._AI_CHAR_LIMITS.get(tier, 2_000)
                if len(ai) > char_limit:
                    logger.warning(
                        "Crystal %s ai_content exceeds soft limit (%d > %d) — keeping full content",
                        tier.value, len(ai), char_limit,
                    )
                return ai, summary
        except Exception:
            logger.warning("LLM crystal compression failed for tier %s", tier.value, exc_info=True)
        return "", ""

    # ── Crystal scoring (RAG) ──────────────────────────────────────
    # Bonus weights mirror the story-card RAG in game_session so that
    # both retrieval layers rank facts the same way.
    _ACTIVE_NPC_BONUS = 50      # crystal mentions an NPC currently in scene
    _LOCATION_BONUS = 30        # crystal mentions current location
    _KEYWORD_MATCH_SCORE = 5    # per query-keyword hit in ai_content
    _RECENCY_BONUS_MAX = 10     # newest unconsumed crystal of its tier gets this

    # Tier base scores: higher tier means broader (and usually older) context,
    # so without query signal we still bias toward recent/specific tiers but
    # never let a tier dominate purely by being old. These are tiebreakers.
    _TIER_BASE_SCORE = {
        CrystalTier.SHORT: 4.0,
        CrystalTier.MEDIUM: 3.0,
        CrystalTier.LONG: 2.0,
    }

    #: Hybrid scoring: how much the provenance signals move the rank. They are
    #: deliberately smaller than the "this NPC is in the scene" bonus (50) so a
    #: well-provenanced memory cannot outrank a scene-critical one, and larger
    #: than a single keyword hit so an important one-off event survives a query
    #: that happens to share more words with a trivial memory.
    _CONFIDENCE_WEIGHT = 6.0
    _IMPORTANCE_WEIGHT = 12.0
    _ENTITY_MATCH_SCORE = 15.0
    _ENTITY_MATCH_CAP = 2
    _TASK_MATCH_SCORE = 15.0
    _TASK_MATCH_CAP = 2
    _OBSERVER_BONUS = 20.0

    def _score_crystal(
        self,
        crystal: MemoryCrystal,
        rank_within_tier: int,
        tier_size: int,
        query_keywords: set[str],
        active_npc_names: set[str],
        location_keywords: set[str],
        scene_entities: set[str] | None = None,
        task_keywords: set[str] | None = None,
    ) -> float:
        """Score a crystal's relevance to the current scene.

        The legacy terms - which the old algorithm used, unchanged, so a
        comparison run is possible - are:

        - Keyword overlap between query and crystal ai_content (weight 5/hit).
        - Active NPC names appearing in the crystal (weight 50/match).
        - Location keywords appearing in the crystal (weight 30/match).
        - Recency within tier: most-recent crystal of its tier gets a small
          bonus, decaying linearly toward 0 for the oldest in that tier.
        - Tier base: small constant per tier as a tiebreaker.

        With MEMORY_SCORING=hybrid (the default) five more signals are added,
        all of them from provenance the crystal already carries:

        - confidence (0..1, weight 6) - a paraphrase is worth less than a record;
        - importance (0..1, weight 12) - a one-off pivotal event must not sink;
        - related_entities matched against the scene (weight 15, at most 2);
        - an active NPC being one of the witnesses (weight 20) - stronger than
          the name merely appearing in the prose;
        - task keywords (weight 15, at most 2).

        Set MEMORY_SCORING=legacy to score exactly as before, which is what the
        baseline comparison test does.

        MEMORY tier crystals are scored only when a query exists; without one
        the caller keeps the historical "include everything" behavior.
        """
        ai = (crystal.ai_content or "").lower()
        if not ai:
            return 0.0

        score = self._TIER_BASE_SCORE.get(crystal.tier, 0.0)

        # Keyword overlap. We tokenize the crystal's ai_content the same way
        # the query is tokenized so that quoted JSON keys ("characters",
        # "events", etc.) become noise-immune via the stop list.
        if query_keywords:
            crystal_words = {token for token in tokenize_text(ai) if token not in _STOP_WORDS}
            hits = len(query_keywords & crystal_words)
            score += hits * self._KEYWORD_MATCH_SCORE

        # Active NPC presence: substring match (NPC names can be multi-word).
        if active_npc_names:
            for name in active_npc_names:
                name = name.strip().lower()
                if name and name in ai:
                    score += self._ACTIVE_NPC_BONUS

        # Location overlap.
        if location_keywords:
            for loc in location_keywords:
                loc = loc.strip().lower()
                if loc and loc in ai:
                    score += self._LOCATION_BONUS

        # Recency within tier: linearly decay from RECENCY_BONUS_MAX (newest)
        # to 0 (oldest). rank_within_tier is 0-based from oldest → newest.
        if tier_size > 1:
            recency = (rank_within_tier / (tier_size - 1)) * self._RECENCY_BONUS_MAX
        else:
            recency = self._RECENCY_BONUS_MAX
        score += recency

        if _scoring_mode() == "legacy":
            return score

        # ── provenance-aware terms (hybrid) ──────────────────────────
        score += self._CONFIDENCE_WEIGHT * max(0.0, min(1.0, float(crystal.confidence or 0.0)))
        score += self._IMPORTANCE_WEIGHT * max(0.0, min(1.0, float(crystal.importance or 0.0)))

        if scene_entities and crystal.related_entities:
            wanted = {name.strip().casefold() for name in scene_entities if name and name.strip()}
            matched = {
                name.strip().casefold()
                for name in crystal.related_entities
                if name and name.strip() and name.strip().casefold() in wanted
            }
            score += min(len(matched), self._ENTITY_MATCH_CAP) * self._ENTITY_MATCH_SCORE

        if active_npc_names:
            observers = {name.strip().casefold() for name in (crystal.witnessed_by or []) if name}
            if observers & {name.strip().casefold() for name in active_npc_names if name}:
                score += self._OBSERVER_BONUS

        if task_keywords:
            matched_task = len(
                {token for token in task_keywords if token} & {token for token in tokenize_text(ai) if token not in _STOP_WORDS}
            )
            score += min(matched_task, self._TASK_MATCH_CAP) * self._TASK_MATCH_SCORE

        return score

    # ── Hybrid recall (P2) ──────────────────────────────────────────

    #: How many candidates the fused ranking may return per tier. The token
    #: budget still decides what reaches the prompt; this only bounds the work.
    _HYBRID_CANDIDATE_LIMIT = 40
    _VECTOR_CACHE_LIMIT = 8

    def _entity_cache_for(self) -> dict:
        cache = getattr(self, "_entity_cache", None)
        if cache is None:
            cache = self._entity_cache = {}
        return cache

    def _entity_index(self, campaign_id: str):
        """Entity index for this campaign, or an empty one without a fact store."""
        cache = self._entity_cache_for()
        hit = cache.get(campaign_id)
        if hit is not None:
            return hit
        index = build_entity_index_from_store(self._facts, campaign_id)
        cache[campaign_id] = index
        return index

    def invalidate_vector_cache(self) -> None:
        """Forget cached semantic channels (after a retraction or a model change)."""
        self._vector_cache.clear()

    def invalidate_entity_cache(self, campaign_id: str | None = None) -> None:
        """Drop cached entity indexes after ingestion taught the store new names."""
        cache = getattr(self, "_entity_cache", None)
        if not cache:
            return
        if campaign_id is None:
            cache.clear()
        else:
            cache.pop(campaign_id, None)

    def _vector_channel(self, campaign_id: str, docs: list):
        """Semantic channel for these crystals, or None when embeddings are off.

        Corpus identity is the doc-id set, not the document list: crystals do
        not change between two turns of the same recency window, and the
        retrieval_embedding layer already caches per (model, corpus size).
        Any failure here means "keyword recall only", never a failed turn.
        """
        corpus = ",".join(sorted(str(getattr(doc, "doc_id", "")) for doc in docs))
        cached = self._vector_cache.get(corpus)
        if cached is not None:
            # A channel that is not usable right now (backend gone) is kept in
            # the cache as None so the next turn does not rebuild it, but the
            # caller must still see "no channel".
            return cached or None
        channel = build_crystal_vector_channel(docs)
        self._vector_cache[corpus] = channel
        if channel is None:
            return None
        if len(self._vector_cache) > self._VECTOR_CACHE_LIMIT:
            self._vector_cache.pop(next(iter(self._vector_cache)))
        return channel


    def _hybrid_recall(
        self,
        campaign_id: str,
        crystals: list[MemoryCrystal],
        *,
        query_text: str,
        active_npc_names: set[str],
        location_keywords: set[str],
        scene_entities: set[str],
        task_keywords: set[str],
    ) -> list[MemoryCrystal] | None:
        """Fuse the retrieval channels for these crystals, or None when off.

        None (not an empty list) means "this path is disabled" - the caller
        then uses the legacy ordering, and a genuinely empty result stays
        distinguishable from a switched-off feature in the trace.
        """
        if _retrieval_mode() != "hybrid":
            return None
        if not crystals:
            return None

        docs = crystal_docs_from_engine(crystals)
        by_id = {
            doc.doc_id: crystal
            for doc, crystal in zip(docs, crystals)
        }
        # Position lookup instead of crystals.index(crystal) inside a
        # comprehension: list.index is linear and compares crystals by value,
        # so a tier of hundreds of similar summaries paid O(n^2) comparisons
        # per cached conversation turn.
        positions = {id(crystal): rank for rank, crystal in enumerate(crystals)}
        tier_size = len(crystals)

        def engine_score(crystal: MemoryCrystal, rank: int) -> float:
            try:
                return self._score_crystal(
                    crystal=crystal,
                    rank_within_tier=rank,
                    tier_size=tier_size,
                    query_keywords=_extract_keywords(query_text),
                    active_npc_names=active_npc_names,
                    location_keywords=location_keywords,
                    scene_entities=scene_entities,
                    task_keywords=task_keywords,
                )
            except (AttributeError, TypeError, ValueError, KeyError):
                logger.warning("crystal scoring failed for %s", crystal.memory_id, exc_info=True)
                return 0.0

        engine_scores = {
            doc_id: engine_score(crystal, positions.get(id(crystal), 0))
            for doc_id, crystal in by_id.items()
        }
        try:
            result = hybrid_rank(
                query_text,
                docs,
                engine_scores=engine_scores,
                facts=fact_docs_from_store(self._facts, campaign_id),
                entity_index=self._entity_index(campaign_id),
                pinned=[doc.doc_id for doc in docs if doc.always_include],
                focus=sorted(scene_entities | set(task_keywords)),
                limit=self._HYBRID_CANDIDATE_LIMIT,
                vector=self._vector_channel(campaign_id, docs),
            )
        except (RuntimeError, ValueError, TypeError, KeyError, LookupError, OSError) as exc:
            # A retrieval bug must not cost the player their recap: fall back
            # to the legacy ordering and record why.
            logger.warning("hybrid recall failed for %s: %s", campaign_id, exc, exc_info=True)
            self._last_retrieval[campaign_id] = {
                "query": query_text,
                "degraded": ["hybrid"],
                "errors": [f"hybrid: {type(exc).__name__}: {exc}"],
            }
            return None

        fused = result.as_dict()
        fused["engine_scores"] = {
            doc_id: round(score, 3) for doc_id, score in engine_scores.items()
        }
        self._last_retrieval[campaign_id] = fused

        ranked = [by_id[doc_id] for doc_id in result.doc_ids() if doc_id in by_id]
        # Chronological again for the narrator, and stable when two crystals
        # share a timestamp (the reason for memory_id in the key).
        ranked.sort(
            key=lambda c: (
                c.source_start_created_at or "",
                c.source_end_created_at or "",
                c.memory_id,
            )
        )
        return ranked

    # ── Context window budget ──────────────────────────────────────
    # Allocate ~10% of provider context window to crystal recall (separate
    # from story cards' 15%). Floor / ceiling keep behavior sane on small
    # providers and prevent runaway on 1M-token windows.
    _CRYSTALS_CONTEXT_FRACTION = 0.10
    _CRYSTALS_MIN_BUDGET_TOKENS = 4_000
    _CRYSTALS_MAX_BUDGET_TOKENS = 100_000

    def _compute_crystals_budget(self, context_window: int) -> int:
        if context_window <= 0:
            return self._CRYSTALS_MIN_BUDGET_TOKENS
        budget = int(context_window * self._CRYSTALS_CONTEXT_FRACTION)
        return max(
            self._CRYSTALS_MIN_BUDGET_TOKENS,
            min(self._CRYSTALS_MAX_BUDGET_TOKENS, budget),
        )

    def _select_ranked_crystals(
        self,
        campaign_id: str,
        tier: CrystalTier,
        query_text: str = "",
        *,
        query_keywords: set[str],
        active_npc_names: set[str],
        location_keywords: set[str],
        token_budget: int,
        scene_entities: set[str] | None = None,
        task_keywords: set[str] | None = None,
    ) -> list[MemoryCrystal]:
        """Score unconsumed crystals of `tier` and return top-N within budget.

        Crystals are ranked by `_score_crystal` - or, with MEMORY_RETRIEVAL=
        hybrid, by the fused retrieval channels - then included greedily until
        the per-tier token budget runs out. Ordering of the returned list is
        chronological (oldest -> newest) so the narrator sees the natural arc.
        """
        unconsumed = self._unconsumed_crystals(campaign_id, tier)
        if not unconsumed:
            return []

        entities = scene_entities or set()
        tasks = task_keywords or set()
        tier_size = len(unconsumed)
        ranked = self._hybrid_recall(
            campaign_id,
            unconsumed,
            query_text=query_text,
            active_npc_names=active_npc_names,
            location_keywords=location_keywords,
            scene_entities=entities,
            task_keywords=tasks,
        )
        if ranked is None:
            scored: list[tuple[float, int, MemoryCrystal]] = []
            for rank, crystal in enumerate(unconsumed):
                score = self._score_crystal(
                    crystal=crystal,
                    rank_within_tier=rank,
                    tier_size=tier_size,
                    query_keywords=query_keywords,
                    active_npc_names=active_npc_names,
                    location_keywords=location_keywords,
                    scene_entities=entities,
                    task_keywords=tasks,
                )
                scored.append((score, rank, crystal))

            # Highest score first; ties broken by recency (higher rank = newer).
            scored.sort(key=lambda x: (-x[0], -x[1]))
            ordered = [crystal for _, _, crystal in scored]
        else:
            ordered = ranked

        selected: list[MemoryCrystal] = []
        used_tokens = 0
        for crystal in ordered:
            cost = estimate_tokens_for_crystal(crystal)
            if used_tokens + cost > token_budget and selected:
                break
            selected.append(crystal)
            used_tokens += cost

        # Restore chronological order for narrator readability. The hybrid
        # path already returns chronological order; sorting again is harmless
        # and leaves the legacy path's behaviour unchanged.
        selected.sort(
            key=lambda c: (c.source_start_created_at or "", c.memory_id)
        )
        return selected

    def _select_permanent_crystals(
        self,
        campaign_id: str,
        *,
        query_text: str = "",
        query_keywords: set[str],
        active_npc_names: set[str],
        location_keywords: set[str],
        scene_entities: set[str],
        task_keywords: set[str],
        token_budget: int,
        rag_on: bool,
    ) -> list[MemoryCrystal]:
        """Pick the MEMORY-tier crystals this turn actually needs.

        The old behavior injected every permanent crystal into every prompt.
        That is what made a long campaign's system prompt grow without bound
        and buried the two lines relevant to the current scene under a hundred
        lines about places the player left forty turns ago.

        Two things are never dropped: a crystal marked `always_include` (the
        caller decided it is canonical) and anything the scene names. The rest
        compete on score inside the same token budget the other tiers use, and
        the result keeps chronological order so the narrator still reads a
        story rather than a search result.

        With no query at all (rag_on False) the historical "include the whole
        tier" behavior is kept: there is nothing to rank against, and the
        generation endpoints rely on the full recap.

        With MEMORY_RETRIEVAL=hybrid the optional crystals are ordered by the
        fused retrieval channels instead of by _score_crystal alone; the
        legacy ordering stays one of those channels, so this widens recall
        rather than replacing it.
        """
        permanent = [
            c for c in self._crystals.get(campaign_id, [])
            if c.tier == CrystalTier.MEMORY
        ]
        if not permanent:
            return []
        if not rag_on:
            return permanent

        pinned = [c for c in permanent if c.always_include]
        optional = [c for c in permanent if not c.always_include]

        # Position map rather than permanent.index(crystal): equality on a
        # dataclass is a 25-field comparison, which turned this loop quadratic
        # on campaigns with many permanent crystals.
        positions = {id(crystal): rank for rank, crystal in enumerate(permanent)}
        used_tokens = 0
        chosen: list[tuple[int, MemoryCrystal]] = []
        for crystal in pinned:
            cost = estimate_tokens_for_crystal(crystal)
            # A pinned memory is kept even when it overflows: dropping canon
            # silently is the one failure this method must not have.
            chosen.append((positions[id(crystal)], crystal))
            used_tokens += cost

        tier_size = len(optional)
        ranked = self._hybrid_recall(
            campaign_id,
            optional,
            query_text=query_text,
            active_npc_names=active_npc_names,
            location_keywords=location_keywords,
            scene_entities=scene_entities,
            task_keywords=task_keywords,
        )
        if ranked is None:
            # MEMORY_RETRIEVAL=score: exactly the pre-P2 ordering.
            scored: list[tuple[float, int, MemoryCrystal]] = []
            for rank, crystal in enumerate(optional):
                score = self._score_crystal(
                    crystal=crystal,
                    rank_within_tier=rank,
                    tier_size=tier_size,
                    query_keywords=query_keywords,
                    active_npc_names=active_npc_names,
                    location_keywords=location_keywords,
                    scene_entities=scene_entities,
                    task_keywords=task_keywords,
                )
                scored.append((score, rank, crystal))
            scored.sort(key=lambda item: (-item[0], -item[1]))
            ordered = [crystal for _, _, crystal in scored]
        else:
            ordered = ranked
        for crystal in ordered:
            cost = estimate_tokens_for_crystal(crystal)
            if used_tokens + cost > token_budget and chosen:
                continue
            chosen.append((positions[id(crystal)], crystal))
            used_tokens += cost

        chosen.sort(key=lambda item: item[0])
        return [crystal for _, crystal in chosen]

    # ── Context window builder ──────────────────────────────────────

    def _render_memory_tiers(
        self,
        campaign_id: str,
        *,
        query_text: str,
        active_npc_names: set[str] | None,
        location: str,
        context_window: int,
        include_permanent: bool,
        task_focus: str,
    ) -> list[str]:
        """The PRMNT/ARC/MID/RCNT blocks, computed once for both builders.

        The two builders used to carry identical copies of this ~60 lines,
        which is how the async path quietly drifted out of step with the sync
        one. Everything that decides *which* crystals are recalled lives here,
        so a change cannot land in only one of them.
        """
        parts: list[str] = []
        rag_on = _rag_crystals_enabled() and bool(
            query_text or active_npc_names or location
        )
        npc_names = {n.lower() for n in (active_npc_names or set()) if n}
        query_keywords = _extract_keywords(query_text)
        location_keywords = _extract_keywords(location)
        # Everything the scene names, for the provenance-aware entity term.
        scene_entities = set(npc_names) | set(location_keywords)
        # Only the context-pack caller knows the open task; without it the
        # task term contributes nothing and ranking is unchanged.
        task_keywords = _extract_keywords(task_focus or "")
        budget = self._compute_crystals_budget(context_window)

        # MEMORY tier. When include_permanent is False the caller renders this
        # tier separately (FASE 2 cached zone).
        if include_permanent:
            permanent = self._select_permanent_crystals(
                campaign_id,
                query_text=query_text,
                query_keywords=query_keywords,
                active_npc_names=npc_names,
                location_keywords=location_keywords,
                scene_entities=scene_entities,
                task_keywords=task_keywords,
                token_budget=budget,
                rag_on=rag_on,
            )
            if permanent:
                parts.append("=== PRMNT_MEM ===")
                for c in permanent:
                    parts.append(c.ai_content)

        tiers = (
            (CrystalTier.LONG, "=== ARC_MEM ==="),
            (CrystalTier.MEDIUM, "=== MID_MEM ==="),
            (CrystalTier.SHORT, "=== RCNT_MEM ==="),
        )
        if rag_on:
            tier_budget = budget // 3
            for tier, header in tiers:
                ranked = self._select_ranked_crystals(
                    campaign_id, tier,
                    query_text=query_text,
                    query_keywords=query_keywords,
                    active_npc_names=npc_names,
                    location_keywords=location_keywords,
                    token_budget=tier_budget,
                    scene_entities=scene_entities,
                    task_keywords=task_keywords,
                )
                if ranked:
                    parts.append(header)
                    for c in ranked:
                        parts.append(c.ai_content)
        else:
            for tier, header in tiers:
                tier_crystals = self._unconsumed_crystals(campaign_id, tier)
                if tier_crystals:
                    parts.append(header)
                    for c in tier_crystals[-3:]:
                        parts.append(c.ai_content)
        return parts
    def build_context_window(
        self,
        campaign_id: str,
        query_text: str = "",
        active_npc_names: set[str] | None = None,
        location: str = "",
        context_window: int = 0,
        include_permanent: bool = True,
        task_focus: str = "",
    ) -> str:
        """Build the WORLD MEMORY section for the narrator's system prompt.

        Pyramid structure — each tier provides progressively broader context:
        - MEMORY: permanent world facts, ranked by relevance to the scene when
          a query exists (pinned crystals are always kept); the whole tier
          only when there is nothing to rank against.
        - LONG / MEDIUM / SHORT: ranked by relevance to (query, NPCs, location)
          when those are provided AND the RAG flag is on. Otherwise falls back
          to the legacy "last 3 unconsumed" behavior (preserves old tests and
          callers that don't have query context, e.g. routes_game generation).
        - DELTA: raw uncrystallized events (last few).
        """
        parts: list[str] = self._render_memory_tiers(
            campaign_id,
            query_text=query_text,
            active_npc_names=active_npc_names,
            location=location,
            context_window=context_window,
            include_permanent=include_permanent,
            task_focus=task_focus,
        )

        # Raw uncrystallized events (the freshest context)
        raw_tail = self._get_uncrystallized_events(campaign_id, limit=10)
        if raw_tail:
            parts.append("=== DELTA ===")
            for event in raw_tail:
                compact = self._event_to_compact_line(event)
                if compact:
                    parts.append(compact)

        return "\n".join(parts)

    async def build_context_window_async(
        self,
        campaign_id: str,
        query_text: str = "",
        active_npc_names: set[str] | None = None,
        location: str = "",
        context_window: int = 0,
        include_permanent: bool = True,
        task_focus: str = "",
    ) -> str:
        """Async version with optional Graphiti fact retrieval. Same RAG semantics."""
        parts: list[str] = self._render_memory_tiers(
            campaign_id,
            query_text=query_text,
            active_npc_names=active_npc_names,
            location=location,
            context_window=context_window,
            include_permanent=include_permanent,
            task_focus=task_focus,
        )
        # The entity query below runs against resolved names; normalise them
        # the same way the tier renderer does instead of leaving None behind.
        active_npc_names = {n.lower() for n in (active_npc_names or set()) if n}

        # Graphiti world facts (if available)
        if self._graphiti:
            raw_tail = self._get_uncrystallized_events(campaign_id, limit=5)
            compact_recent = [self._event_to_compact_line(event) for event in raw_tail]
            recent_text = " ".join(line for line in compact_recent if line)
            # The graph indexes entities, not paragraphs. Searching with the
            # whole event tail (the previous behaviour) matched almost nothing,
            # because Graphiti compares the query against indexed node names;
            # so the query names the active NPCs and the current location, and
            # only falls back to the tail when no entity is known.
            graph_query_text = build_graph_query(
                location=location,
                active_npcs=active_npc_names,
                player_input=query_text,
            ).text()
            if not graph_query_text:
                graph_query_text = build_memory_query(
                    query_text=query_text,
                    location=location,
                    active_npcs=active_npc_names,
                    recent_events=recent_text,
                ).text()
            if graph_query_text:
                try:
                    facts = await self._graphiti.search(campaign_id, graph_query_text, limit=8)
                    if facts:
                        parts.append("=== WORLD FACTS ===")
                        for f in facts:
                            parts.append(f"- {f['fact']}")
                except (RuntimeError, OSError, ValueError, TypeError, KeyError, LookupError) as exc:
                    # The graph is an optional channel: a dead Neo4j must not
                    # take the memory window with it, but it must be visible.
                    logger.warning(
                        "Graphiti fact recall failed for campaign %s: %s", campaign_id, exc
                    )

        raw_tail = self._get_uncrystallized_events(campaign_id, limit=10)
        if raw_tail:
            parts.append("=== DELTA ===")
            for event in raw_tail:
                compact = self._event_to_compact_line(event)
                if compact:
                    parts.append(compact)

        return "\n".join(parts)

    # ── NPC perspective filter (Camada 3) ──────────────────────────

    def _resolve_crystal_audiences(self, campaign_id: str) -> None:
        """Restore trusted policy metadata or verify legacy raw provenance, failing closed."""
        unresolved = [c for c in self._crystals.get(campaign_id, []) if not c.audience_resolved]
        if not unresolved:
            return
        for crystal in unresolved:
            crystal.witnessed_by = []
        try:
            persisted = self._store.get_by_type(campaign_id, EventType.MEMORY_CRYSTAL, limit=-1)
            trusted: dict[tuple, set[str]] = {}
            for event in persisted:
                payload = event.payload
                if payload.get("audience_policy") != self.AUDIENCE_POLICY:
                    continue
                key = (
                    payload.get("tier"), payload.get("summary"), payload.get("ai_content"),
                    payload.get("event_count"), payload.get("source_start_created_at"),
                    payload.get("source_end_created_at") or event.created_at,
                )
                audience = _normalized_witnesses(payload.get("witnessed_by"))
                trusted[key] = trusted[key] & audience if key in trusted else audience

            legacy = []
            for crystal in unresolved:
                key = (
                    crystal.tier.value, crystal.content, crystal.ai_content,
                    crystal.event_count, crystal.source_start_created_at, crystal.source_end_created_at,
                )
                if key in trusted:
                    crystal.witnessed_by = sorted(trusted[key])
                    crystal.audience_resolved = True
                else:
                    legacy.append(crystal)

            with_spans = [
                c for c in legacy
                if isinstance(c.source_start_created_at, str) and c.source_start_created_at
                and isinstance(c.source_end_created_at, str) and c.source_end_created_at
                and isinstance(c.event_count, int) and c.event_count > 0
                and c.source_start_created_at <= c.source_end_created_at
            ]
            if with_spans:
                raw_events = self._store.get_after(
                    campaign_id, limit=-1, event_types=list(self.CRYSTALLIZE_EVENT_TYPES),
                )
                for crystal in with_spans:
                    sources = [
                        event for event in raw_events
                        if crystal.source_start_created_at <= event.created_at <= crystal.source_end_created_at
                    ]
                    if (
                        len(sources) == crystal.event_count
                        and sources[0].created_at == crystal.source_start_created_at
                        and sources[-1].created_at == crystal.source_end_created_at
                    ):
                        crystal.witnessed_by = _common_witnesses(sources)
                        crystal.witnessed_names = _display_witnesses(sources, crystal.witnessed_by)
            for crystal in legacy:
                crystal.audience_resolved = True
        except Exception:
            logger.warning("Could not verify crystal audiences for campaign %s", campaign_id, exc_info=True)

    def build_npc_knowledge_window(
        self,
        campaign_id: str,
        npc_name: str,
        perspective: "NpcPerspective | None" = None,
    ) -> str:
        """Return only verified witnessed summaries and a bounded, witnessed recent raw tail.

        When a perspective is supplied, the fact block is added *after* the
        witness filter and *from* that perspective only, so this character sees
        its own claims - including ones the world contradicts - and nothing
        else. Building the facts here rather than in a prompt template is the
        P3 rule: permission filtering must happen before the context exists.
        """
        name_key = npc_name.strip().casefold() if npc_name else ""
        if not name_key:
            return ""
        self._resolve_crystal_audiences(campaign_id)
        crystals = self._crystals.get(campaign_id, [])
        parts: list[str] = []

        for tier, header in (
            (CrystalTier.MEMORY, "=== PRMNT_MEM (witnessed) ==="),
            (CrystalTier.LONG, "=== ARC_MEM (witnessed) ==="),
            (CrystalTier.MEDIUM, "=== MID_MEM (witnessed) ==="),
            (CrystalTier.SHORT, "=== RCNT_MEM (witnessed) ==="),
        ):
            tier_crystals = [
                c for c in crystals
                if c.tier == tier
                and (tier == CrystalTier.MEMORY or not c.consumed)
                and name_key in _normalized_witnesses(c.witnessed_by)
            ]
            if tier_crystals:
                parts.append(header)
                for c in tier_crystals:
                    parts.append(c.ai_content)

        # Keep witnessed raw recall even when a mixed-audience summary cannot be shared.
        raw_tail = [
            event for event in self._store.get_recent(campaign_id, limit=self.MAX_CRYSTALLIZE_EVENTS)
            if event.event_type in self.CRYSTALLIZE_EVENT_TYPES
        ][-self.NPC_RAW_EVENT_LIMIT:]
        witnessed_raw = [ev for ev in raw_tail if name_key in _normalized_witnesses(ev.witnessed_by)]
        if witnessed_raw:
            parts.append("=== DELTA (witnessed) ===")
            for event in witnessed_raw:
                compact = self._event_to_compact_line(event)
                if compact:
                    parts.append(compact)

        # Facts last: they are conclusions drawn from everything above, and the
        # prompt reads top-down. A perspective already carries the permission
        # decision, so the only work left here is rendering.
        if perspective is not None:
            my_facts = self._npc_facts(campaign_id, npc_name)
            lines = facts_for_prompt(my_facts, observer=npc_name, perspective=perspective)
            if lines:
                parts.append("=== FACTS (yours to know) ===")
                parts.extend(lines)

        return "\n".join(parts)

    def _npc_facts(self, campaign_id: str, npc_name: str) -> list:
        """Live facts this campaign has, before any per-NPC filtering."""
        if self._facts is None:
            return []
        try:
            return self._facts.find_facts(campaign_id) or []
        except (RuntimeError, OSError, ValueError, TypeError, KeyError, LookupError) as exc:
            # Fact storage is optional: a broken memory.db must not cost the NPC
            # the witnessed summaries it can still legitimately see.
            logger.warning("Fact recall failed for campaign %s: %s", campaign_id, exc)
            return []

    def build_npc_perspective(
        self,
        campaign_id: str,
        npc_name: str,
        beliefs: "list | None" = None,
    ) -> "NpcPerspective | None":
        """Assemble one character's perspective from facts, not from prose.

        The world facts are filtered to what this NPC may observe *here*, so a
        caller cannot pass an unfiltered list and expect the prompt layer to
        catch it later.
        """
        name_key = (npc_name or "").strip()
        if not name_key:
            return None
        decided = filter_facts_for_observer(self._npc_facts(campaign_id, name_key), name_key)
        return build_perspective(
            name_key,
            facts=decided,
            observations=self._witnessed_event_ids(campaign_id, name_key),
            beliefs=list(beliefs or ()) + self.stored_beliefs(campaign_id, name_key),
        )

    def stored_beliefs(self, campaign_id: str, npc_name: str) -> list:
        """Claims this character has been told, read back from its own mind.

        The NPC mind already stores per-character thoughts as key/value pairs;
        a thought key of the form "belief:<predicate>:<subject>" is a claim the
        character holds and is the one write path this layer needs, because the
        dialogue and rumour code can record "the duke was told Eileen is dead"
        without a second database. Unreadable thoughts degrade to no claims: a
        character with no beliefs is the normal case, not an error.
        """
        if self._minds is None:
            return []
        try:
            mind = self._minds.get_mind(campaign_id, npc_name)
        except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
            logger.debug("no stored beliefs for %s: %s", npc_name, exc)
            return []
        thoughts = getattr(mind, "thoughts", None) or {}
        claims: list = []
        for key, thought in thoughts.items():
            text_key = str(key or "")
            if not text_key.lower().startswith("belief:"):
                continue
            # "belief:<predicate>:<subject>[:<optional note>]" - the subject
            # and predicate are both free text, so only the first three fields
            # are structural; a label after them is ignored rather than parsed.
            parts = text_key.split(":")
            if len(parts) < 3:
                continue
            _, predicate, subject = parts[0], parts[1], parts[2]
            value = str(getattr(thought, "value", thought) or "").strip()
            if not (subject.strip() and predicate.strip() and value):
                continue
            claims.append(
                Claim(
                    subject_id=subject.strip(),
                    predicate=predicate.strip(),
                    object_value=value,
                    stance=BELIEVES,
                    confidence=0.5,
                    note="told to them",
                )
            )
        return claims

    def _witnessed_event_ids(self, campaign_id: str, npc_name: str, limit: int = 40) -> list:
        name_key = npc_name.strip().casefold() if npc_name else ""
        if not name_key or self._store is None:
            return []
        try:
            recent = self._store.get_recent(campaign_id, limit=self.MAX_CRYSTALLIZE_EVENTS)
        except (RuntimeError, OSError, ValueError, TypeError, KeyError, LookupError):
            return []
        out = []
        for event in recent:
            if event.event_type in self.CRYSTALLIZE_EVENT_TYPES and name_key in _normalized_witnesses(event.witnessed_by):
                out.append(event.id)
        return out[-limit:]

    # ── Persistence ─────────────────────────────────────────────────

    def _persist_crystal(self, campaign_id: str, crystal: MemoryCrystal) -> None:
        try:
            self._store.append(
                campaign_id=campaign_id,
                event_type=EventType.MEMORY_CRYSTAL,
                payload={
                    "tier": crystal.tier.value,
                    "summary": crystal.content,
                    "ai_content": crystal.ai_content,
                    "event_count": crystal.event_count,
                    "consumed": crystal.consumed,
                    "witnessed_by": list(crystal.witnessed_by or []),
                    "audience_policy": self.AUDIENCE_POLICY if crystal.audience_resolved else None,
                    # Real raw-event span, so rebuild restores the true crystal
                    # cursor instead of jumping to this event's persist time.
                    "source_start_created_at": crystal.source_start_created_at,
                    "source_end_created_at": crystal.source_end_created_at,
                    # Provenance: why this memory exists, who it belongs to and
                    # how far to trust the wording.
                    "memory_id": crystal.memory_id,
                    "source": crystal.source,
                    "observer": crystal.observer,
                    "confidence": crystal.confidence,
                    "importance": crystal.importance,
                    "visibility": crystal.visibility,
                    "related_entities": list(crystal.related_entities or []),
                    "status": crystal.status,
                    # Theme-driven consolidation: which thread this crystal
                    # belongs to and which summaries it replaced, so a merged
                    # paraphrase stays traceable to its sources.
                    "theme_key": crystal.theme_key,
                    "theme_keywords": list(crystal.theme_keywords or []),
                    "source_memory_ids": list(crystal.source_memory_ids or []),
                },
                narrative_time_delta=0,
                location="memory",
                entities=[],
            )
        except Exception:
            logger.warning(
                "Failed to persist %s crystal for campaign %s",
                crystal.tier.value, campaign_id, exc_info=True,
            )

    # ── Event helpers ───────────────────────────────────────────────

    def _get_uncrystallized_events(self, campaign_id: str, limit: int) -> list[Event]:
        cursor = self._last_crystal_cursor.get(campaign_id)
        return self._store.get_after(
            campaign_id=campaign_id,
            after_created_at=cursor,
            limit=limit,
            event_types=list(self.CRYSTALLIZE_EVENT_TYPES),
        )

    def _event_to_compact_line(self, event: Event) -> str:
        type_code = {
            EventType.PLAYER_ACTION: "PA",
            EventType.NARRATOR_RESPONSE: "NR",
            EventType.WORLD_TICK: "WT",
            EventType.COMBAT_RESULT: "CR",
            EventType.TIMESKIP: "TS",
        }.get(event.event_type, event.event_type.value[:2])

        if event.event_type == EventType.TIMESKIP:
            seconds = int(event.payload.get("seconds", 0) or 0)
            return f"{type_code}:{seconds}s"

        text = str(event.payload.get("text", "")).replace("\n", " ").strip()
        if not text and event.event_type == EventType.COMBAT_RESULT:
            outcome = str(event.payload.get("outcome", "")).strip()
            quality = event.payload.get("quality", "")
            text = f"{outcome}/{quality}"

        if not text:
            return ""

        if len(text) > 160:
            text = text[:157] + "..."
        return f"{type_code}:{text}"

    def _format_event_batch(self, events: list[Event]) -> str:
        lines: list[str] = []
        for event in events:
            compact = self._event_to_compact_line(event)
            if compact:
                lines.append(compact)
        return "\n".join(lines) if lines else "(no relevant events)"

    # ── Fallbacks ───────────────────────────────────────────────────

    def _fallback_short(self, events: list[Event]) -> str:
        """Fallback SHORT crystal when LLM compression fails.

        Produces a minimal structured-JSON entry that matches the crystal
        schema so downstream consumers parse it the same way as a normal
        LLM-generated crystal. No lossy truncation — every unique event
        line survives.
        """
        fallback_events: list[dict] = []
        seen: set[str] = set()
        for event in events:
            line = self._event_to_compact_line(event)
            if not line:
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            fallback_events.append({
                "who": "",
                "action": line,
                "where": getattr(event, "location", "") or "",
                "result": "",
            })
        payload = {
            "events": fallback_events,
            "characters": {},
            "items": [],
            "promises_or_missions": [],
            "world_facts": [],
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _fallback_player_summary(events: list[Event]) -> str:
        snippets: list[str] = []
        seen: set[str] = set()
        for event in events[-8:]:
            text = str(event.payload.get("text", "")).strip()
            if text:
                normalized = text.replace("\n", " ")
                key = normalized.lower()
                if key in seen:
                    continue
                seen.add(key)
                snippets.append(normalized)
        if not snippets:
            return "No significant events were recorded recently."
        joined = " ".join(snippets)
        if len(joined) > 480:
            joined = joined[:477] + "..."
        return joined
