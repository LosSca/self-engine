"""Web search is an enhancement: every failure path must stay quiet."""
from __future__ import annotations

from app.services import web_search as ws

PAGE = """
<div class="result results_links">
  <h2><a rel="nofollow" class="result__a"
    href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FBeowulf&amp;rut=abc">Beowulf
    - Wikipedia</a></h2>
  <a class="result__snippet" href="#">Beowulf is an <b>Old English</b> epic poem.</a>
</div>
<div class="result results_links">
  <h2><a rel="nofollow" class="result__a" href="https://example.org/x">Second &amp; last</a></h2>
</div>
"""


def test_parse_unwraps_redirect_and_strips_tags():
    hits = ws.parse_results(PAGE, 5)
    assert [h["url"] for h in hits] == [
        "https://en.wikipedia.org/wiki/Beowulf",
        "https://example.org/x",
    ]
    assert hits[0]["title"] == "Beowulf - Wikipedia"
    assert hits[0]["snippet"] == "Beowulf is an Old English epic poem."
    # A result without a snippet is kept, not dropped.
    assert hits[1]["snippet"] == ""


def test_parse_honours_the_limit_and_empty_pages():
    assert len(ws.parse_results(PAGE, 1)) == 1
    assert ws.parse_results("", 5) == []


def test_format_notes_is_bounded():
    hits = [{"title": "T%d" % i, "url": "u", "snippet": "x" * 200} for i in range(10)]
    notes = ws.format_notes(hits, chars=120)
    assert len(notes) == 120
    assert notes.startswith("- T0: ")


async def test_search_without_a_query_never_calls_the_network():
    assert await ws.web_search("   ") == []
