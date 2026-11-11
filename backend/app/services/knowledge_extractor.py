"""Turn the prose a turn actually produced into authored knowledge.

This is a proposal lane, not a state lane:

* it reads the narrative text of committed turns and proposes candidate
  knowledge entries; it never edits the world, the story line or the state;
* nothing is stored unless the caller passes apply=True, and even then the
  entries only land in scenario_knowledge, which the prompt builder treats
  as canon on the next turn and nothing more - no rule reads knowledge, so a
  wrong proposal degrades narration instead of corrupting a save;
* extraction is deterministic (sentences the engine itself wrote), so the
  same transcript always yields the same proposals and the tests need no
  model. A model is optional and separates facts from atmosphere; a broken
  model reply is reported in warnings, never guessed at.

Every proposal keeps the request_id and turn it came from, so the author can
jump back to the turn that justified it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.models.knowledge import KnowledgeEntryCreate

PROPOSAL_LAYERS: Tuple[str, ...] = ("character", "scene", "event", "global")
SENTENCE_SPLIT = re.compile("(?<=[.!?。！？])|(?<=\n)")
CJK_RE = re.compile("[一-鿿]")
MIN_TITLE = 2
MAX_TITLE = 80
MAX_CONTENT = 400
DEFAULT_LIMIT = 20


FACT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"^The (?P<a>[A-Z][\w'\- ]{1,40}?) is (?:a|an|the) (?P<b>[^.]{2,80})\.$", "global"),
    (r"^(?P<a>[A-Z][\w'\- ]{1,40}?) has (?P<b>[^.]{2,80})\.$", "global"),
    (r"^(?P<a>[^\uff0c\u3002]{1,24})\u662f(?P<b>[^\u3002]{2,60})\u3002$", "global"),
    (r"^(?P<a>[^\uff0c\u3002]{1,24})\u6301\u6709(?P<b>[^\u3002]{2,60})\u3002$", "global"),
)


def split_sentences(text: str) -> List[str]:
    """Split prose into trimmed, non-empty sentences."""
    parts = SENTENCE_SPLIT.split(text or "")
    return [p.strip() for p in parts if p and p.strip()]


def classify_sentence(sentence: str) -> Optional[str]:
    """Pick the layer a sentence belongs to, or None when it is noise.

    Questions are never canon (the narrator asking itself something cannot be
    quoted back as a fact). A sentence that talks to the player belongs to the
    player's dossier; anything else is an event of the scene.
    """
    text = (sentence or "").strip()
    if len(text) < MIN_TITLE:
        return None
    if text.endswith(("?", "\uff1f")):
        return None
    if CJK_RE.search(text):
        if "\u4f60" in text:
            return "character"
        return "event"
    lowered = text.lower()
    if " you " in lowered or lowered.startswith("you "):
        return "character"
    return "event"


def fact_of(sentence: str) -> Optional[Tuple[str, str]]:
    """Return (subject, predicate) when the sentence states a whole fact."""
    for pattern, _layer in FACT_PATTERNS:
        match = re.match(pattern, (sentence or "").strip())
        if match:
            return (match.group("a").strip(), match.group("b").strip())
    return None


def turn_texts(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only the rows that actually carry narrative text."""
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        payload = row.get("payload") or {}
        text = str(payload.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "text": text,
            "request_id": str(row.get("request_id") or ""),
            "turn": int(row.get("state_version") or row.get("version") or row.get("turn") or 0),
        })
    return out


def _proposal(sentence: str, layer: str, source: Dict[str, Any]) -> Dict[str, Any]:
    subject = fact_of(sentence)
    return {
        "title": (subject[0] if subject else sentence)[:MAX_TITLE],
        "content": sentence[:MAX_CONTENT],
        "layer": layer,
        "is_fact": subject is not None,
        "source_request_id": str(source.get("request_id") or ""),
        "source_turn": int(source.get("turn") or 0),
        "tags": ["extracted"],
        "depth": 6,
        "sort_order": 0,
    }


def proposals_from_turns(turns: List[Dict[str, Any]], limit: int = DEFAULT_LIMIT) -> List[Dict[str, Any]]:
    """Deterministic proposals: one per new (layer, sentence), in reading order."""
    out: List[Dict[str, Any]] = []
    taken = set()
    for turn in turns or []:
        for sentence in split_sentences(str(turn.get("text") or "")):
            layer = classify_sentence(sentence)
            if not layer:
                continue
            key = (layer, sentence.casefold())
            if key in taken:
                continue
            taken.add(key)
            out.append(_proposal(sentence, layer, turn))
            if len(out) >= limit:
                return out
    return out


def _existing_keys(entries: Iterable[Any]) -> set:
    """Fingerprints of the entries already authored, for de-duplication."""
    keys = set()
    for entry in entries or []:
        title = str(getattr(entry, "title", "") or "").strip()
        content = str(getattr(entry, "content", "") or "").strip()
        layer = str(getattr(entry, "layer", "") or "").strip()
        if content:
            keys.add((layer, content.casefold()))
        if title:
            keys.add((layer, title.casefold()))
    return keys


def plan_import(
    proposals: Sequence[Dict[str, Any]],
    existing: Iterable[Any] = (),
    layer: Optional[str] = None,
) -> Tuple[List[KnowledgeEntryCreate], List[str]]:
    """Turn proposals into validatable entries, skipping what is already known.

    Returns (entries, warnings). A proposal that cannot be validated is
    reported and skipped; it never aborts the batch, because one bad sentence
    should not cost the author the other ten.
    """
    warnings: List[str] = []
    keys = _existing_keys(existing)
    entries: List[KnowledgeEntryCreate] = []
    used_titles: Dict[str, int] = {}
    for index, proposal in enumerate(proposals or [], start=1):
        if not isinstance(proposal, dict):
            warnings.append("proposal %d is not an object" % index)
            continue
        content = str(proposal.get("content") or "").strip()
        title = str(proposal.get("title") or content)[:MAX_TITLE].strip()
        chosen = layer or str(proposal.get("layer") or "")
        if chosen not in PROPOSAL_LAYERS:
            chosen = "event"
        if not content:
            warnings.append("proposal %d has no content" % index)
            continue
        if (chosen, content.casefold()) in keys:
            continue
        count = used_titles.get(title.casefold(), 0)
        if count:
            title = (title[: MAX_TITLE - 4] + " (" + str(count + 1) + ")").strip()
        used_titles[title.casefold()] = count + 1
        try:
            entry = KnowledgeEntryCreate(
                layer=chosen,
                title=title or content[:MAX_TITLE],
                content=content,
                tags=[str(tag) for tag in (proposal.get("tags") or []) if str(tag).strip()],
                depth=int(proposal.get("depth", 6)),
                sort_order=int(proposal.get("sort_order", 0)),
            )
        except Exception as exc:  # noqa: BLE001 - a bad proposal is data, not a crash
            warnings.append("proposal %d was rejected: %s" % (index, exc))
            continue
        keys.add((chosen, content.casefold()))
        entries.append(entry)
    return entries, warnings


def apply_import(store: Any, scenario_id: str, entries: Sequence[KnowledgeEntryCreate]) -> List[str]:
    """Write the planned entries; returns the ids actually created."""
    created: List[str] = []
    for entry in entries or []:
        try:
            record = store.add_knowledge(scenario_id, entry)
        except Exception:  # noqa: BLE001 - surfaced by the caller as a warning
            raise
        created.append(getattr(record, "id", ""))
    return created


REFINE_SYSTEM = (
    "You polish knowledge entries for a text RPG. You do not invent facts: "
    "you only rewrite the sentence you are given so it reads as a stable note "
    "about the world. Reply with JSON only."
)


def build_refine_prompt(proposals: Sequence[Dict[str, Any]], language: str = "zh") -> str:
    lines = [
        "Rewrite each item as a short canon note. Keep every proper noun.",
        "Return JSON: {\"items\": [{\"title\": str, \"content\": str}]} with the same order.",
        "Language: " + language,
        "Items:",
    ]
    for index, proposal in enumerate(proposals or [], start=1):
        lines.append("%d. %s" % (index, str(proposal.get("content") or "")[:MAX_CONTENT]))
    return "\n".join(lines)


def _parse_items(text: str) -> List[Dict[str, Any]]:
    import json

    raw = (text or "").strip()
    if not raw:
        return []
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001 - a malformed reply is data, not a crash
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


async def refine_proposals(
    router: Any,
    proposals: Sequence[Dict[str, Any]],
    language: str = "zh",
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Ask the model to polish wording; on any failure keep the originals."""
    warnings: List[str] = []
    items = list(proposals or [])
    if not items or router is None:
        return items, warnings
    try:
        reply = await router.complete(
            [{"role": "system", "content": REFINE_SYSTEM}, {"role": "user", "content": build_refine_prompt(items, language)}],
            max_tokens=1600,
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001 - refinement is optional
        warnings.append("refinement failed: %s" % exc)
        return items, warnings
    refined = _parse_items(str(reply or ""))
    if len(refined) != len(items):
        warnings.append("refinement returned %d items for %d proposals; kept the originals" % (len(refined), len(items)))
        return items, warnings
    out: List[Dict[str, Any]] = []
    for original, patch in zip(items, refined):
        merged = dict(original)
        title = str(patch.get("title") or "").strip()
        content = str(patch.get("content") or "").strip()
        if title:
            merged["title"] = title[:MAX_TITLE]
        if content:
            merged["content"] = content[:MAX_CONTENT]
        out.append(merged)
    return out, warnings


async def extract_from_campaign(
    game_store: Any,
    scenario_store: Any,
    scenario_id: str,
    campaign_id: str,
    *,
    router: Any = None,
    apply: bool = False,
    limit: int = DEFAULT_LIMIT,
    language: str = "zh",
) -> Dict[str, Any]:
    """Read a campaign's committed narration and propose knowledge entries.

    Only rows the engine already committed are read (event_type
    "narrator.response"), so the proposals are grounded in what the player was
    actually told. Nothing is written unless apply is True, and even then only
    the scenario knowledge table is touched.
    """
    rows = game_store.list_events(campaign_id, limit=200)
    turns = turn_texts([row for row in rows if str(row.get("event_type")) == "narrator.response"])
    proposals = proposals_from_turns(turns, limit=limit)
    warnings: List[str] = []
    if router is not None and proposals:
        proposals, refine_warnings = await refine_proposals(router, proposals, language)
        warnings.extend(refine_warnings)
    existing = scenario_store.list_knowledge(scenario_id)
    entries, plan_warnings = plan_import(proposals, existing)
    warnings.extend(plan_warnings)
    created: List[str] = []
    if apply and entries:
        written, write_warnings = _write_entries(scenario_store, scenario_id, entries)
        created.extend(written)
        warnings.extend(write_warnings)
    return {
        "scenario_id": scenario_id,
        "campaign_id": campaign_id,
        "turns_read": len(turns),
        "proposals": proposals,
        "entries": [entry.model_dump() for entry in entries],
        "created_ids": created,
        "applied": bool(apply),
        "warnings": warnings,
    }


def _write_entries(
    store: Any, scenario_id: str, entries: Sequence[KnowledgeEntryCreate]
) -> Tuple[List[str], List[str]]:
    """Insert the planned entries, reporting each failure without aborting."""
    created: List[str] = []
    warnings: List[str] = []
    for entry in entries:
        try:
            record = store.add_knowledge(scenario_id, entry)
        except Exception as exc:  # noqa: BLE001 - one bad row must not lose the rest
            warnings.append("could not store '%s': %s" % (entry.title, exc))
            continue
        created.append(str(getattr(record, "id", "")))
    return created, warnings
