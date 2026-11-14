"""Shared test isolation.

后端把设置、剧本、事件都放在 backend 目录下的 sqlite 文件里。测试如果没有重定向，
就会读到开发者本机真实账号的设置（例如全局写作指令），断言随之变成环境依赖。
这里让每个测试都跑在自己的临时数据库上。
"""

import pytest

#: 每个世界都必须有开场白（CreateScenarioRequest 校验）。测试里绝大多数场景
#: 并不关心开场文本，用这句占位即可，别把用例写成「没有开场白也算合法」。
DEFAULT_OPENING = "测试开场：你在一间点着灯的房间里醒来。"


def make_scenario(client, **fields):
    """按必填字段建一个情景，返回响应 JSON；失败时把响应体带进断言。"""
    payload = {"title": "Test World", "opening_narrative": DEFAULT_OPENING}
    payload.update(fields)
    resp = client.post("/api/scenarios/", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.fixture
def client():
    """一个走真实路由的 TestClient；数据库由 isolated_databases 指向临时文件。"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_databases(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENARIO_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("EVENT_DB_PATH", str(tmp_path / "events.db"))
