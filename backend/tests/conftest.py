"""Shared test isolation.

后端把设置、剧本、事件都放在 backend 目录下的 sqlite 文件里。测试如果没有重定向，
就会读到开发者本机真实账号的设置（例如全局写作指令），断言随之变成环境依赖。
这里让每个测试都跑在自己的临时数据库上。

Furthermore, this host's filesystem sandbox refuses directories created with an
explicit ``0o700`` mode — what pytest does for every ``tmp_path`` and what
``tempfile.mkdtemp`` does — so the shim at the top of this file drops that
argument (a no-op on Windows anyway, where the ACL decides) and points TEMP at
a directory inside the checkout. Nothing in the product code is involved.
"""

import os
import tempfile

import pytest

_original_mkdir = os.mkdir


def _mkdir_ignoring_mode(path, mode=0o777, *, dir_fd=None):
    if mode == 0o700 and dir_fd is None:
        return _original_mkdir(path)
    return _original_mkdir(path, mode, dir_fd=dir_fd)


_mkdir_ignoring_mode._dsh_sandbox_safe = True  # type: ignore[attr-defined]

if not getattr(_original_mkdir, '_dsh_sandbox_safe', False):
    os.mkdir = _mkdir_ignoring_mode

#: Where pytest keeps its scratch directories. The first candidate inside the
#: checkout wins, because the sandbox already allows writes there.
_TEMP_CANDIDATES = (
    os.path.join(os.getcwd(), '.tmp', 'pytest'),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.tmp', 'pytest'),
)


def _usable_temp_root():
    for candidate in _TEMP_CANDIDATES:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, '.probe')
            with open(probe, 'w', encoding='utf-8') as handle:
                handle.write('x')
            os.remove(probe)
            return candidate
        except OSError:
            continue
    return None


_root = _usable_temp_root()
if _root:
    os.environ['TEMP'] = _root
    os.environ['TMP'] = _root
    os.environ['PYTEST_DEBUG_TEMPROOT'] = _root
    tempfile.tempdir = _root

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
    monkeypatch.setenv("GAME_STATE_DB_PATH", str(tmp_path / "game_state.db"))
    monkeypatch.setenv("LLM_TRACE_DB_PATH", str(tmp_path / "traces.db"))
    #: 事实层是可丢弃的派生数据，测试里同样不该落到开发者本机的 memory.db。
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))


# --- shared helpers other test modules import -----------------------------
#
# These live below the sandbox shim so the import order stays obvious: a test
# module that does `from tests.conftest import DEFAULT_OPENING` must get a
# conftest whose mkdir shim is already installed.

#: 每个世界都必须有开场白（CreateScenarioRequest 校验）。测试里绝大多数场景
#: 并不关心开场文本，用这句占位即可，别把用例写成「没有开场白也算合法」。
DEFAULT_OPENING = "测试开场：你在一间点着灯的房间里醒来。"


@pytest.fixture
def client():
    """一个走真实路由的 TestClient；数据库由 isolated_databases 指向临时文件。"""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_databases(tmp_path):
    """把每个测试的数据库钉在自己的临时目录里。

    Autouse：否则断言会读到开发者本机真实的设置、情景与事件，测试就变成
    环境依赖而不是行为描述。用 MonkeyPatch 而不是直接改 os.environ，测试
    结束时自动还原，不会漏给下一个用例。
    """
    values = {
        "SCENARIO_DB_PATH": tmp_path / "scenarios.db",
        "EVENT_DB_PATH": tmp_path / "events.db",
        "GAME_STATE_DB_PATH": tmp_path / "game_state.db",
        "MEMORY_DB_PATH": tmp_path / "memory.db",
        "LLM_TRACE_DB_PATH": tmp_path / "traces.db",
        "PREFS_DB_PATH": tmp_path / "prefs.db",
    }
    patcher = pytest.MonkeyPatch()
    for name, path in values.items():
        patcher.setenv(name, str(path))
    yield
    patcher.undo()


def make_scenario(client, **fields):
    """按必填字段建一个情景，返回响应 JSON；失败时把响应体带进断言。"""
    payload = {"title": "Test World", "opening_narrative": DEFAULT_OPENING}
    payload.update(fields)
    resp = client.post("/api/scenarios/", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()
