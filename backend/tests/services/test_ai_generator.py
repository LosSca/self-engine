"""AI drafting validates model output before it can be saved."""
import json

import pytest

from app.services.ai_generator import (
    GenerationError,
    generate_knowledge,
    generate_storyline,
    generate_world,
)


class StubRouter:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return "```json\n" + json.dumps(self.payload, ensure_ascii=False) + "\n```"


@pytest.mark.asyncio
async def test_knowledge_draft_validates_entries():
    router = StubRouter(
        {
            "entries": [
                {"layer": "global", "title": "群岛", "content": "雾", "tags": ["world"]},
                {"layer": "character", "title": "守卫", "content": "忠于旧王"},
            ]
        }
    )
    drafts = await generate_knowledge(router, "海岛世界", 2)
    assert [item["title"] for item in drafts] == ["群岛", "守卫"]
    assert router.calls and "JSON" in router.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_storyline_draft_reports_validation_warnings():
    router = StubRouter(
        {
            "name": "线",
            "lines": [{"id": "trunk", "title": "主线", "kind": "main"}],
            "nodes": [
                {"id": "start", "title": "开始", "kind": "main", "line": "trunk"},
                {"id": "side", "title": "暗线", "kind": "side", "line": "trunk"},
            ],
        }
    )
    draft = await generate_storyline(router, "测试", 2)
    assert draft["storyline"]["name"] == "线"
    assert draft["warnings"]


@pytest.mark.asyncio
async def test_world_draft_overrides_ids_and_rejects_broken_refs():
    good = {
        "world_id": "ignored",
        "scenario_id": "ignored",
        "name": "小岛",
        "start_location_id": "a",
        "locations": [{"id": "a", "name": "A"}],
        "items": [],
        "doors": [],
        "entities": [],
        "quests": [],
        "events": [],
    }
    draft = await generate_world(StubRouter(good), "小岛", "sc", "ai_test")
    assert draft["world"].world_id == "ai_test"
    assert draft["world"].scenario_id == "sc"

    broken = dict(good, locations=[{"id": "a", "name": "A", "exits": [{"target_id": "ghost"}]}])
    with pytest.raises(GenerationError) as exc:
        await generate_world(StubRouter(broken), "坏世界", "sc", "ai_bad")
    assert "ghost" in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_json_is_reported():
    class Bad:
        async def complete(self, messages, **kwargs):
            return "not json"

    with pytest.raises(GenerationError):
        await generate_knowledge(Bad(), "x", 1)
