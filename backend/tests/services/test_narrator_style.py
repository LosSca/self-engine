"""设置面板里的风格开关必须真的进 prompt，而不是只存下来。"""
from __future__ import annotations

from app.services import action_service


def test_style_notes_translate_known_switches():
    notes = action_service.narrator_style_notes({
        "narrator_style": "gothic",
        "narrator_perspective": "third",
        "narrator_length": "short",
        "narrator_show_rules": True,
        "narrator_allow_ooc": True,
        "narrator_language_hint": "像说书人",
    })
    joined = "\n".join(notes)
    assert "阴郁" in joined
    assert "第三人称" in joined
    assert "2 段以内" in joined
    assert "判定结果" in joined
    assert "场外提示" in joined
    assert "像说书人" in joined


def test_style_notes_ignore_unknown_values():
    assert action_service.narrator_style_notes({
        "narrator_style": "nonsense",
        "narrator_length": "",
        "narrator_language_hint": "   ",
    }) == []


def test_style_notes_read_the_store_when_no_mapping_is_passed(monkeypatch):
    monkeypatch.setattr(action_service, "_prefs", lambda: {"narrator_style": "pulp"})
    assert action_service.narrator_style_notes() == [
        "通俗冒险小说节奏：悬念密集、每段都推进冲突。"
    ]
