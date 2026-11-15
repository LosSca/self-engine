"""README 里那些数得出来的数字，必须和代码对得上。

README 曾经写着「123 条路径 / 159 个操作 / 24 个 engines 模块 / 2039 条测试」，
而实际是 130 / 166 / 30 / 2063 —— 这种数字不会自己过期地「漂移」，只会被
一次改写忘掉另一处。测试比人可靠：这里把每个数从代码里数出来，和 README 对齐。
数字改了、README 没跟上，这条测试就会红。

只校验「能从代码确定答案」的量；主观描述（架构、特性）不在此列。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
README = REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def _api_counts() -> tuple[int, int]:
    """（唯一路径数, 操作数），取自 FastAPI 自己生成的 OpenAPI 文档。"""
    from app.main import app

    spec = app.openapi()
    paths = spec["paths"]
    operations = sum(len(route) for route in paths.values())
    return len(paths), operations


def _prefix_counts() -> dict[str, int]:
    from app.main import app

    counts: dict[str, int] = {}
    for path in app.openapi()["paths"]:
        segments = path.split("/")
        key = "/".join(segments[:3]) if len(segments) > 3 else path
        counts[key] = counts.get(key, 0) + 1
    return counts


def _engine_modules() -> int:
    return len(list((BACKEND_DIR / "app" / "engines").glob("*.py")))


def _test_files() -> int:
    # 排除这个守门文件自己：它一出生就让 README 的数字多 1，那不是被审查的量。
    here = Path(__file__).resolve()
    return len([p for p in (BACKEND_DIR / "tests").rglob("test_*.py") if p.resolve() != here])


def _actions() -> int:
    from app.engines import action_registry

    return len(action_registry.all_actions())


def test_readme_api_totals_match_the_openapi_document(readme):
    paths, operations = _api_counts()
    assert f"**{paths} 条 API 路径**" in readme, f"README 没写 {paths} 条 API 路径"
    assert f"（{operations} 个操作）" in readme, f"README 没写 {operations} 个操作"


def test_readme_prefix_table_matches_the_routers(readme):
    counts = _prefix_counts()
    total = sum(counts.values())
    # 概览表里的总行
    assert f"共 **{total} 条路径" in readme or f"**{total} 条 API 路径**" in readme
    for prefix, count in counts.items():
        if count < 2:
            # 单路由前缀在 README 里合并成一行，跳过大表格断言
            continue
        pattern = re.compile(r"\|\s*`" + re.escape(prefix) + r"`\s*\|\s*(\d+)\s*\|")
        match = pattern.search(readme)
        assert match, f"README 的 API 表里没有 {prefix}"
        assert int(match.group(1)) == count, (
            f"{prefix} 实际 {count} 条，README 写的是 {match.group(1)}"
        )


def test_readme_engine_module_count_matches_the_directory(readme):
    modules = _engine_modules()
    assert f"engines/             # 规则与引擎层（{modules} 个模块）" in readme
    assert f"**engines/**（{modules} 个模块）" in readme


def test_readme_test_file_count_matches_the_suite(readme):
    files = _test_files()
    assert f"{files} 个测试文件" in readme


def test_readme_action_count_matches_the_registry(readme):
    actions = _actions()
    assert f"当前 **{actions} 个行动**" in readme
    assert f"内置行动目录（{actions} 个行动，含中文动词）" in readme


def test_readme_does_not_contain_a_stale_test_total(readme):
    """README 不写死 passed 数时跳过；一旦写了就必须是全绿时实测的那个数。

    这里只守住「不能出现旧数字」：2039 是上一轮的数字，已经从代码里涨走了。
    """
    assert "2039 passed" not in readme
