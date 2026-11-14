"""Bing is the fallback engine; its parser and the safety filter need pinning."""
from __future__ import annotations

from app.services import web_search as ws

BING = """
<ol id="b_results">
 <li class="b_algo" data-id iid=SERP.1><link rel="stylesheet" href="https://r.bing.com/x.css"/>
   <h2><a href="https://en.wikipedia.org/wiki/Beowulf">Beowulf - Wikipedia</a></h2>
   <p class="b_lineclamp">Beowulf is an <strong>Old English</strong> epic poem.</p></li>
 <li class="b_algo"><h2><a href="https://www.pornhub.com/video">Should be dropped</a></h2>
   <p>nope</p></li>
 <li class="b_algo"><h2><a href="https://example.org/second">Second hit</a></h2>
   <p>no snippet class here</p></li>
</ol>
"""


def test_parse_bing_keeps_organic_hits_and_drops_blocked_hosts():
    hits = ws.parse_bing(BING, 5)
    assert [h["url"] for h in hits] == [
        "https://en.wikipedia.org/wiki/Beowulf",
        "https://example.org/second",
    ]
    assert hits[0]["title"] == "Beowulf - Wikipedia"
    assert hits[0]["snippet"] == "Beowulf is an Old English epic poem."
    assert hits[1]["snippet"] == "no snippet class here"


def test_parse_bing_ignores_pages_without_results():
    assert ws.parse_bing("", 5) == []


def test_parse_results_drops_blocked_hosts_too():
    page = (
        '<a class="result__a" href="https://xhamster.com/a">a</a>'
        '<a class="result__a" href="https://en.wikipedia.org/wiki/B">B</a>'
    )
    assert [h["url"] for h in ws.parse_results(page, 5)] == ["https://en.wikipedia.org/wiki/B"]
