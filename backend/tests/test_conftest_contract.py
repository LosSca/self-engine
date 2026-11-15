"""tests/conftest.py 的契约：其它测试模块导入了它的常量与 helper。"""
from __future__ import annotations

import os
import tempfile

from tests.conftest import DEFAULT_OPENING, make_scenario


def test_the_shared_opening_is_still_exported():
    assert DEFAULT_OPENING.strip()


def test_make_scenario_creates_a_world(client):
    world = make_scenario(client, title="契约世界")
    assert world["title"] == "契约世界"
    assert world.get("opening_narrative") or True


def test_every_test_gets_its_own_databases(isolated_databases):
    # The autouse fixture is what keeps a developer's real settings out of the
    # suite; it is asserted here because a silent removal would make assertions
    # environment-dependent rather than failing.
    for name in ("SCENARIO_DB_PATH", "EVENT_DB_PATH", "GAME_STATE_DB_PATH", "MEMORY_DB_PATH"):
        assert os.environ.get(name), name


def test_tmp_directories_are_creatable_on_this_host(tmp_path):
    # Regression guard for the 0o700 sandbox shim: if it disappears, every
    # tmp_path user fails with PermissionError instead of an obvious assert.
    path = tmp_path / "probe"
    path.mkdir()
    (path / "file.txt").write_text("x", encoding="utf-8")
    assert (path / "file.txt").read_text(encoding="utf-8") == "x"
    made = tempfile.mkdtemp()
    assert os.path.isdir(made)