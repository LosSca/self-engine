"""全局写作指令的回落规则。

用户希望「写一句风格要求」在换世界之后仍然生效；世界自己写了指令时，
世界优先，全局只当兜底。这里锁住读取与容错，容错尤其重要：
设置只是风格偏好，不能让一局游戏因为读设置出问题而中断。
"""

import pytest


@pytest.fixture
def prefs(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    from app.db import prefs_store

    store = prefs_store.PrefsStore(db_path=str(tmp_path / "scenarios.db"))
    monkeypatch.setattr(prefs_store, "get_prefs_store", lambda: store)
    yield store
    store.close()


def test_reads_the_directive_the_user_saved(prefs):
    from app.services.action_service import default_narrator_prompt

    prefs.put({"default_narrator_prompt": "用短句，别抒情。"})
    assert default_narrator_prompt() == "用短句，别抒情。"


def test_is_empty_when_the_user_never_wrote_one(prefs):
    from app.services.action_service import default_narrator_prompt

    assert default_narrator_prompt() == ""


def test_survives_a_broken_store(monkeypatch):
    from app.db import prefs_store
    from app.services.action_service import default_narrator_prompt

    def boom():
        raise RuntimeError("db is gone")

    monkeypatch.setattr(prefs_store, "get_prefs_store", boom)
    assert default_narrator_prompt() == ""
