"""Draft/expand endpoints: search enrichment must be optional and silent."""
from __future__ import annotations

from app.services import draft_generator as dg


class _DeadRouter:
    async def complete(self, messages, **kwargs):
        raise RuntimeError("no key")


class _RecordingRouter:
    def __init__(self, reply="drafted"):
        self.messages = None
        self.reply = reply

    async def complete(self, messages, **kwargs):
        self.messages = messages
        return self.reply


async def test_draft_uses_search_notes_when_asked(monkeypatch):
    seen = {}

    async def fake_search(query, *, limit=None):
        seen["query"] = query
        return [{"title": "Beowulf", "url": "https://example.org", "snippet": "Old English epic."}]

    monkeypatch.setattr("app.services.web_search.web_search", fake_search)
    router = _RecordingRouter()
    text, provider = await dg.generate_draft("character", topic="贝奥武夫", router=router, search=True)

    assert (text, provider) == ("drafted", "llm")
    assert seen["query"].startswith("贝奥武夫")
    prompt = router.messages[-1]["content"]
    assert "WEB NOTES" in prompt
    assert "Old English epic." in prompt


async def test_draft_without_search_leaves_notes_out(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("search must not run when the flag is off")

    monkeypatch.setattr("app.services.web_search.web_search", boom)
    router = _RecordingRouter()
    await dg.generate_draft("world", topic="北境", router=router)
    assert "WEB NOTES" not in router.messages[-1]["content"]


async def test_expand_continues_the_existing_text():
    router = _RecordingRouter("and then the bell rang.")
    text, provider = await dg.expand_draft("character", text="他站在门口。", router=router)
    assert (text, provider) == ("and then the bell rang.", "llm")
    assert "他站在门口。" in router.messages[-1]["content"]


async def test_expand_falls_back_to_a_template_without_a_model():
    text, provider = await dg.expand_draft("character", text="x", router=_DeadRouter())
    assert provider == "template"
    assert text == dg.template_expansion("character")
    assert await dg.expand_draft("character", text="   ", router=None) == (
        dg.template_expansion("character"),
        "template",
    )
