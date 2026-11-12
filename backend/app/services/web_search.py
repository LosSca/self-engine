"""Keyless web search for authoring.

The character library and the scenario builder let an author type a name and ask
for a draft. Often the name is a real person, place, myth or game world, and the
draft is far better when it starts from facts instead of the model's memory.

There is no search API key here on purpose. Three keyless HTML endpoints are
tried in order — DuckDuckGo's no-JS page, DuckDuckGo lite, Bing's HTML results —
and SEARCH_BASE_URL may point at a self-hosted SearxNG instead. Each engine
rate-limits or serves a challenge page on its own schedule, which is the whole
reason for a chain: one engine answering 202 costs a few hundred milliseconds,
not the feature.

Every failure returns an empty list. Search is an enhancement, never a
dependency, so authoring keeps working offline.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from html import unescape
from typing import Dict, List
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
)

#: Two engines parse the same way: an <a class="result__a"> per hit with an
#: optional <a class="result__snippet"> beside it. Parsing two tags beats pulling
#: in an HTML parser for a page this regular.
_RESULT_LINK = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.S,
)
_SNIPPET = re.compile(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<text>.*?)</a>', re.S)
#: Bing wraps each organic hit in <li class="b_algo"> with the heading link first.
_BING_ITEM = re.compile(r'<li class="b_algo".*?</li>', re.S)
_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
_BING_SNIPPET = re.compile(r'<p[^>]*>(?P<text>.*?)</p>', re.S)
_TAG = re.compile(r"<[^>]+>")

#: Hosts that must never end up in a prompt. Search engines do not filter unless
#: asked to, and the point of this module is worldbuilding material for a
#: narrative engine, so a small blocklist is a safety net rather than a policy
#: engine: it only drops obvious adult hosts from the parsed hits.
_BLOCKED_HOST_PARTS = (
    "porn",
    "hentai",
    "xnxx",
    "xvideos",
    "bokep",
    "xhamster",
    "redtube",
    "spankbang",
)


def _is_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(part in host for part in _BLOCKED_HOST_PARTS)

#: httpx parses NO_PROXY by splitting on "," and then on ":", so an entry like
#: "::1" becomes host "" + port "1]" and *every* request dies with
#: "InvalidURL: Invalid port". Windows users routinely have exactly that value,
#: so the proxy environment is normalised for the duration of a call instead of
#: asking every user to fix their shell.
_NO_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def _clean_proxy_env() -> dict:
    """Strip httpx-hostile entries; returns the original values for restoring.

    On Windows os.environ is case-insensitive, so NO_PROXY and no_proxy are the
    *same* slot: every original has to be read before anything is written, or
    the restore puts the cleaned value back.
    """
    originals = {key: os.environ[key] for key in _NO_PROXY_KEYS if key in os.environ}
    for key, raw in originals.items():
        kept = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            # Drop bare IPv6 literals ("::1") and bracketed ones ("[::1]"): httpx
            # cannot parse either, and localhost is covered by the defaults below.
            if ":" in part.strip("[]"):
                continue
            kept.append(part)
        os.environ[key] = ",".join(kept) or "localhost,127.0.0.1"
    return originals


def _restore_proxy_env(saved: dict) -> None:
    for key, value in saved.items():
        os.environ[key] = value


def _text(fragment: str) -> str:
    return " ".join(unescape(_TAG.sub(" ", fragment or "")).split())


def _clean_url(href: str) -> str:
    """DuckDuckGo redirects through /l/?uddg=<url>; unwrap to the real target."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


def parse_results(html: str, limit: int = 5) -> List[Dict[str, str]]:
    """Extract {title, url, snippet} from a DuckDuckGo HTML result page."""
    titles = list(_RESULT_LINK.finditer(html or ""))
    snippets = [_text(m.group("text")) for m in _SNIPPET.finditer(html or "")]
    out: List[Dict[str, str]] = []
    for index, match in enumerate(titles[: max(1, limit)]):
        title = _text(match.group("title"))
        url = _clean_url(match.group("href"))
        if not title or not url or _is_blocked(url):
            continue
        out.append(
            {
                "title": title,
                "url": url,
                "snippet": snippets[index] if index < len(snippets) else "",
            }
        )
    return out


def parse_bing(html: str, limit: int = 5) -> List[Dict[str, str]]:
    """Extract {title, url, snippet} from a Bing HTML results page."""
    out: List[Dict[str, str]] = []
    for block in _BING_ITEM.findall(html or "")[: max(1, limit)]:
        link = _BING_LINK.search(block)
        if not link:
            continue
        title = _text(link.group("title"))
        url = unescape(link.group("href"))
        if not title or not url.startswith("http") or _is_blocked(url):
            continue
        snippet_match = _BING_SNIPPET.search(block)
        out.append(
            {
                "title": title,
                "url": url,
                "snippet": _text(snippet_match.group("text"))[:400] if snippet_match else "",
            }
        )
    return out


async def _fetch(
    client: httpx.AsyncClient, method: str, url: str, data: dict | None
) -> httpx.Response:
    # Without a declared language Bing serves whatever its edge cache holds for
    # the exit IP: a zh-CN box in another region can return results for an
    # unrelated query. Asking for en-US/cc=US keeps the page aligned with what
    # was typed, and the titles still come back in the topic's own language.
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }
    if method == "POST":
        return await client.post(url, data=data or {}, headers=headers)
    return await client.get(url, headers=headers)


async def _one_engine(query: str, cap: int) -> List[Dict[str, str]]:
    """Ask each keyless engine in turn until one returns parsable hits."""
    base = (settings.search_base_url or "").strip()
    engines = []
    if base:
        # An explicit endpoint is the operator's choice: use it alone (SearxNG
        # and friends already aggregate), and parse it as DuckDuckGo HTML.
        engines.append(("custom", "POST", base, parse_results))
    else:
        engines = [
            ("duckduckgo-html", "POST", "https://html.duckduckgo.com/html/", parse_results),
            ("duckduckgo-lite", "GET", "https://lite.duckduckgo.com/lite/?q={q}", parse_results),
            ("bing", "GET", "https://www.bing.com/search?q={q}&setlang=en-US&cc=US", parse_bing),
        ]

    async with httpx.AsyncClient(timeout=settings.search_timeout_seconds, follow_redirects=True) as client:
        for name, method, url, parser in engines:
            target = url.format(q=query)
            try:
                response = await _fetch(client, method, target, {"q": query, "kl": "wt-wt"})
            except Exception as exc:  # noqa: BLE001 - fall through to the next engine
                logger.info("web search engine %s failed for %r: %s", name, query, exc)
                continue
            if response.status_code >= 400:
                logger.info("web search engine %s returned HTTP %s", name, response.status_code)
                continue
            hits = parser(response.text, cap)
            if hits:
                return hits
            logger.info("web search engine %s returned no parsable results", name)
    return []


async def web_search(query: str, *, limit: int | None = None) -> List[Dict[str, str]]:
    """Search the web; empty list on any failure (offline, blocked, timeout)."""
    query = " ".join((query or "").split())
    if not query:
        return []
    cap = max(1, min(limit or settings.search_max_results, 10))
    saved = _clean_proxy_env()
    try:
        hits = await _one_engine(query, cap)
        if hits:
            return hits
        # Every engine refusing at once is usually a burst limit, not a dead
        # network: one spaced retry is cheap and often succeeds.
        await asyncio.sleep(1.5)
        return await _one_engine(query, cap)
    except Exception as exc:  # noqa: BLE001 - search must never break authoring
        logger.info("web search failed for %r: %s", query, exc)
        return []
    finally:
        _restore_proxy_env(saved)


def format_notes(results: List[Dict[str, str]], *, chars: int = 1200) -> str:
    """Flatten hits into the compact block handed to a prompt."""
    lines = []
    for hit in results:
        snippet = hit.get("snippet") or ""
        lines.append("- " + hit.get("title", "") + ": " + snippet)
    return "\n".join(lines)[:chars]
