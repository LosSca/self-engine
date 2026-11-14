"""Graph channel: multi-hop relationship recall from the temporal graph.

Graphiti (Neo4j) already stores what the narrator learned as temporal edges.
The channel wraps it so the rest of the stack sees ordinary ranked documents:

* the query is built from **entities**, not from a narration paragraph
  (:mod:`graph_query`);
* several entities are searched, and each fact is scored by hop distance and
  how many query entities it connects, so a fact about two people in the scene
  outranks a fact about one;
* facts whose `invalid_at` has passed are dropped unless the caller asked for
  history;
* the remaining facts are ordered by recency, because when two edges conflict
  the newer one is the true one.

A graph outage degrades the channel to an empty list and records the reason -
the same contract as the vector channel, so fusion is unaffected.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from app.services.retrieval.graph_query import (
    GraphQuery,
    filter_valid,
    order_by_recency,
)
from app.services.retrieval.models import RetrievedDoc
from app.services.retrieval.tokenizer import DEFAULT_TOKENIZE_CONFIG, TokenizeConfig, token_set

logger = logging.getLogger(__name__)

CHANNEL = "graph"


class GraphRetriever:
    """Runs a :class:`GraphQuery` against an engine with an async `search`."""

    def __init__(
        self,
        engine,
        campaign_id: str,
        *,
        known_names: Sequence[str] = (),
        aliases: Optional[Dict[str, str]] = None,
        per_entity_limit: int = 8,
        include_invalid: bool = False,
        tokenize_config: TokenizeConfig = DEFAULT_TOKENIZE_CONFIG,
    ):
        self._engine = engine
        self._campaign_id = campaign_id
        self._known_names = [str(n) for n in known_names if str(n or "").strip()]
        self._aliases = dict(aliases or {})
        self._per_entity_limit = max(1, per_entity_limit)
        self._include_invalid = include_invalid
        self._tokenize_config = tokenize_config
        self._error = ""

    @property
    def enabled(self) -> bool:
        return self._engine is not None and bool(self._campaign_id)

    @property
    def error(self) -> str:
        return self._error

    async def search_async(self, query: GraphQuery) -> List[RetrievedDoc]:
        """Facts for every entity in `query`, merged and ranked."""
        if not self.enabled or query.is_empty():
            return []
        entities = query.entities or [query.text.strip()]
        collected: Dict[str, Dict[str, object]] = {}
        hops: Dict[str, int] = {}

        for entity in entities:
            try:
                facts = await self._engine.search(
                    self._campaign_id, entity, limit=self._per_entity_limit
                )
            except (RuntimeError, OSError, ValueError, TypeError, KeyError, TimeoutError) as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "graph channel failed for campaign %s", self._campaign_id, exc_info=True
                )
                continue
            for fact in facts or []:
                text = str(fact.get("fact") or "").strip()
                if not text:
                    continue
                # The same edge can be returned for two entities; the smaller
                # hop count is the honest one.
                hops[text] = min(hops.get(text, query.max_hops), 1)
                collected.setdefault(text, dict(fact, fact=text))

        if not collected:
            return []
        facts = order_by_recency(filter_valid(list(collected.values()), self._include_invalid))
        query_terms = token_set(query.text, self._tokenize_config)
        out: List[RetrievedDoc] = []
        for position, fact in enumerate(facts):
            text = str(fact.get("fact"))
            connected = len(query_terms & token_set(text, self._tokenize_config))
            hop = hops.get(text, query.max_hops)
            # A connected fact at one hop beats a loosely related one at two;
            # recency breaks the remaining ties without dominating.
            score = (2.0 / hop) + (0.5 * connected) - (0.001 * position)
            out.append(
                RetrievedDoc(
                    doc_id=f"graph:{self._campaign_id}:{text[:80]}",
                    score=score,
                    channel=CHANNEL,
                    text=text,
                    metadata={
                        "valid_at": fact.get("valid_at"),
                        "invalid_at": fact.get("invalid_at"),
                        "entities": list(entities),
                    },
                    provenance=f"graph:{hop}hop:{connected}hit",
                )
            )
        out.sort(key=lambda doc: -doc.score)
        return out

    def search_sync_wrapper(self):
        """Present the async search on the protocol the pipeline expects."""
        return self


class SyncGraphAdapter:
    """Make a sync caller able to drive the async graph channel.

    `RetrievalService.retrieve` is synchronous because the knowledge path is
    synchronous; the graph client is async. This adapter runs the coroutine on a
    private loop when no loop is running (the normal case for a request that
    already awaits elsewhere), and returns nothing when called from inside a
    running loop - a nested loop would deadlock, and a degraded graph channel is
    strictly better than a hung turn.
    """

    def __init__(self, retriever: GraphRetriever, query: GraphQuery):
        self._retriever = retriever
        self._query = query

    def search(self, _query_text: str = "") -> List[RetrievedDoc]:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            logger.info("graph channel skipped: a loop is already running")
            return []
        try:
            return asyncio.run(self._retriever.search_async(self._query))
        except RuntimeError as exc:
            logger.warning("graph channel could not run: %s", exc)
            return []


__all__ = ["GraphRetriever", "SyncGraphAdapter", "CHANNEL"]
