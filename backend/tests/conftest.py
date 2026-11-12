"""Shared test isolation.

后端把设置、剧本、事件都放在 backend 目录下的 sqlite 文件里。测试如果没有重定向，
就会读到开发者本机真实账号的设置（例如全局写作指令），断言随之变成环境依赖。
这里让每个测试都跑在自己的临时数据库上。
"""

import pytest


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
