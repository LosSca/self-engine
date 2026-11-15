"""桌面壳：一个端口同时提供界面与 API。

打包后的窗口不应该依赖"两个端口 + 一条代理规则"：那三样东西只要有一处不同步，
窗口里就是一片白。这里验证的是后端自己托管构建产物的那条路：
根路径给 index.html、静态资源按真实类型返回、前端路由回落到 index.html、
/api 不会被界面吞掉、路径穿越读不到仓库里的文件。

挂载发生在 app.main **第一次 import** 时，而这个模块在整个测试会话里只初始化
一次；因此这几条用例在独立进程里跑，而不是和别的测试共用同一个 app 实例。
"""
import os
import subprocess
import sys
from pathlib import Path


def _run_shell(tmp_path: Path, dist: Path | None) -> list[str]:
    code = r"""
import json
from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)
page = c.get("/")
asset = c.get("/assets/app.js")
deep = c.get("/studio")
health = c.get("/api/health")
leak = c.get("/../../../../etc/passwd")
print(json.dumps({
    "root": page.status_code,
    "title": "<title>" in page.text,
    "asset": asset.status_code,
    "asset_type": asset.headers.get("content-type", ""),
    "deep": deep.status_code,
    "health": health.status_code,
    "leak_is_app": leak.text.lstrip().startswith("<!doctype html>"),
}))
"""
    env = {k: v for k, v in os.environ.items() if k != "SELF_ENGINE_DESKTOP_DIST"}
    env.update(
        {
            "SCENARIO_DB_PATH": str(tmp_path / "scenarios.db"),
            "GAME_STATE_DB_PATH": str(tmp_path / "game_state.db"),
            "EVENT_DB_PATH": str(tmp_path / "events.db"),
            "ENGINE_GRAPH_MIRROR": "0",
        }
    )
    if dist is not None:
        env["SELF_ENGINE_DESKTOP_DIST"] = str(dist)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert out.returncode == 0, out.stderr[-1500:]
    return out.stdout.strip().splitlines()[-1]


def _build(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>自指引擎</title>", encoding="utf-8"
    )
    (dist / "assets" / "app.js").write_text("console.log('shell')", encoding="utf-8")
    return dist


def test_the_window_serves_the_app_and_the_api_on_one_port(tmp_path):
    import json

    result = json.loads(_run_shell(tmp_path, _build(tmp_path)))

    assert result["root"] == 200
    assert result["title"] is True
    assert result["asset"] == 200
    # A shell that serves every file as text/html makes the browser refuse the
    # module script and the window stays blank.
    assert "javascript" in result["asset_type"]
    # A client-side route is not a file: the app, not a 404, must come back.
    assert result["deep"] == 200
    assert result["health"] == 200


def test_the_shell_cannot_read_files_outside_the_build(tmp_path):
    import json

    result = json.loads(_run_shell(tmp_path, _build(tmp_path)))

    # Anything that is not a real file in dist gets index.html, never the disk.
    assert result["leak_is_app"] is True


def test_without_the_variable_no_shell_is_mounted(tmp_path):
    """A normal launch.py run must not serve a stale build at /."""
    import json

    result = json.loads(_run_shell(tmp_path, None))

    assert result["root"] == 404
    assert result["health"] == 200
