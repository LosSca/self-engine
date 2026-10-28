"""自指引擎启动器 / Self-Reference Engine launcher.

由 start.bat 以 UTF-8 控制台调用：start.bat 只做纯 ASCII 的解释器探测，
所有中文提示与进程编排都在这里完成，避免 cmd.exe 代码页把 .bat 里的
UTF-8 中文读坏（这是旧版 start.bat 在本机直接报“系统找不到指定的路径”
并闪退的根本原因）。

窗口模式不使用 "cmd /c <含引号的长命令>"：Windows 的 argv 转义会把内部
引号变成 \"，cmd.exe 会把它当成字面反斜杠引号而报“不是内部或外部命令”。
改为用本文件自我重启（python launch.py --run-backend），参数以列表传给
CreateProcess，完全绕开 cmd.exe 的引号解析。

用法：
    python launch.py                # 启动后端 + 前端并打开浏览器
    python launch.py --debug        # 后端 --reload + debug 日志，转储 LLM 调用
    python launch.py --backend-only
    python launch.py --frontend-only
    python launch.py --no-browser
    python launch.py --no-window    # 不开新窗口，输出写 .logs/（用于自检/CI）
    python launch.py --run-backend  # 内部使用：在新控制台里跑后端
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# 控制台/管道统一按 UTF-8 输出，避免 GBK 控制台把中文提示写成乱码。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
LOGS = ROOT / ".logs"

BACKEND_PORT = 8000
FRONTEND_PORT = 5173
HEALTH_URL = "http://127.0.0.1:%d/api/health" % BACKEND_PORT
APP_URL = "http://127.0.0.1:%d" % FRONTEND_PORT

CREATE_NEW_CONSOLE = 0x00000010

LABELS = {"backend": "后端", "frontend": "前端"}
LOG_NAMES = {"backend": "backend.log", "frontend": "frontend.log"}

BANNER = """
========================================
  自指引擎 Self-Reference Engine
  规则决定事实 · 事件记录变化 · 状态保存结果 · 模型只负责叙事
========================================
"""


def say(msg: str = "") -> None:
    print(msg, flush=True)


def find_python() -> str | None:
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = BACKEND / rel
        if candidate.exists():
            return str(candidate)
    if sys.executable:
        return sys.executable
    return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, OSError):
        return False


def wait_until(predicate, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            say(" 就绪")
            return True
        print(".", end="", flush=True)
        time.sleep(interval)
    say(" 超时")
    return False


def child_env(debug: bool) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if debug:
        env["LUNAR_DUMP_LLM_CALLS"] = "1"
    return env


def backend_command(py: str, debug: bool) -> list[str]:
    level = "debug" if debug else "info"
    cmd = [py, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT), "--log-level", level]
    if debug:
        cmd.insert(3, "--reload")
    return cmd


def service_command(kind: str, py: str, debug: bool) -> tuple[list[str], Path]:
    """单个服务的启动命令与工作目录。"""
    if kind == "backend":
        return backend_command(py, debug), BACKEND
    # npm 是 npm.cmd，CreateProcess 不能直接执行 .cmd，统一交给 cmd 解析（此处无引号，安全）。
    return ["cmd", "/c", "npm run dev"], FRONTEND


def log_path(kind: str) -> Path:
    path = LOGS / LOG_NAMES[kind]
    try:
        LOGS.mkdir(exist_ok=True)
    except OSError:
        path = Path(tempfile.gettempdir()) / "lunar-logs" / LOG_NAMES[kind]
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def spawn_service(kind: str, py: str, debug: bool, no_window: bool) -> subprocess.Popen:
    """启动一个服务进程。默认开独立窗口（关闭窗口即停止该服务），--no-window 时写日志。"""
    label = LABELS[kind]
    env = child_env(debug)
    if no_window:
        cmd, cwd = service_command(kind, py, debug)
        path = log_path(kind)
        handle = open(path, "ab", buffering=0)  # noqa: SIM115 - 生命周期交给子进程
        say("  · %s -> %s" % (label, path))
        return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL)

    relaunch = [sys.executable, str(Path(__file__).resolve()), "--run-" + kind]
    if debug:
        relaunch.append("--debug")
    say("  · %s -> 独立窗口「自指引擎 - %s」" % (label, label))
    return subprocess.Popen(relaunch, cwd=str(ROOT), env=env, creationflags=CREATE_NEW_CONSOLE)


def run_service(kind: str, debug: bool) -> int:
    """在独立控制台窗口内实际运行一个服务，退出后停住窗口以便查看报错。"""
    label = LABELS[kind]
    try:  # 只在真正的控制台里有效；重定向时会抛异常，忽略即可。
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("自指引擎 - %s" % label)
    except Exception:  # noqa: BLE001 - 标题设置失败不应阻断启动
        pass

    py = find_python() or sys.executable
    cmd, cwd = service_command(kind, py, debug)
    say("=" * 44)
    say(" 自指引擎 · %s" % label)
    say("=" * 44)
    say("命令: %s" % " ".join(cmd))
    say("目录: %s" % cwd)
    say("")
    code = subprocess.call(cmd, cwd=str(cwd), env=child_env(debug))
    say("")
    say("[%s 已退出，退出码 %s]" % (label, code))
    say("关闭本窗口不影响其他服务；重新启动请再次双击 start.bat。")
    try:
        input("按回车关闭本窗口...")
    except EOFError:
        pass
    return code


def print_failure_help(what: str) -> None:
    say()
    say("[失败] %s 没有在预期时间内就绪。" % what)
    if not (FRONTEND / "node_modules").is_dir():
        say("  · frontend/node_modules 不存在：先执行  cd frontend && npm install")
    say("  · 查看对应窗口里的报错；--no-window 模式见 .logs/")
    say("  · 依赖缺失时执行  backend\\.venv\\Scripts\\python.exe -m pip install -r backend\\requirements.txt")
    say("  · 端口被占用时用  netstat -ano | findstr :8000  （:5173 同理）排查")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="自指引擎启动器")
    parser.add_argument("--backend-only", action="store_true", help="只启动后端")
    parser.add_argument("--frontend-only", action="store_true", help="只启动前端")
    parser.add_argument("--debug", action="store_true", help="后端 --reload + debug 日志，且转储 LLM 调用")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--no-window", action="store_true", help="不开新窗口，输出写 .logs/（自检用）")
    parser.add_argument("--run-backend", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-frontend", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # 内部模式：本进程就是这个服务（由窗口模式自我重启产生）。
    if args.run_backend:
        return run_service("backend", args.debug)
    if args.run_frontend:
        return run_service("frontend", args.debug)

    say(BANNER)

    py = find_python()
    if not py:
        say("[错误] 找不到 Python 解释器，请先运行 install.bat。")
        return 1
    say("[环境] 解释器: %s" % py)
    say("[环境] 后端目录: %s" % BACKEND)
    say("[环境] 前端目录: %s" % FRONTEND)

    want_backend = not args.frontend_only
    want_frontend = not args.backend_only
    children: list[subprocess.Popen] = []
    started_backend = False
    started_frontend = False

    # ---- 后端 ----
    if want_backend:
        if http_ok(HEALTH_URL):
            say("[1/3] 后端已在运行: %s" % HEALTH_URL)
        elif port_open(BACKEND_PORT):
            say("[错误] 端口 %d 已被其他进程占用，但它不是本项目的后端。" % BACKEND_PORT)
            say("       请关闭占用进程后重试：netstat -ano | findstr :%d" % BACKEND_PORT)
            print_failure_help("后端")
            return 1
        else:
            say("[1/3] 启动后端: %s" % HEALTH_URL)
            children.append(spawn_service("backend", py, args.debug, args.no_window))
            started_backend = True
    else:
        say("[1/3] 跳过后端（--frontend-only）")

    # ---- 前端 ----
    if want_frontend:
        if port_open(FRONTEND_PORT):
            say("[2/3] 前端已在运行: %s" % APP_URL)
        elif not (FRONTEND / "node_modules").is_dir():
            say("[错误] frontend/node_modules 不存在，请先执行  cd frontend && npm install")
            print_failure_help("前端")
            return 1
        else:
            say("[2/3] 启动前端: %s" % APP_URL)
            children.append(spawn_service("frontend", py, args.debug, args.no_window))
            started_frontend = True
    else:
        say("[2/3] 跳过前端（--backend-only）")

    # ---- 等待就绪 ----
    ok = True
    if started_backend:
        say("[3/3] 等待后端就绪")
        if not wait_until(lambda: http_ok(HEALTH_URL), 120):
            print_failure_help("后端")
            ok = False
    if started_frontend:
        say("[3/3] 等待前端就绪")
        if not wait_until(lambda: port_open(FRONTEND_PORT), 120):
            print_failure_help("前端")
            ok = False

    if not ok:
        say()
        say("服务未能全部就绪，请按上面的提示排查。")
        return 1

    say()
    say("========================================")
    say("  已就绪：")
    if want_frontend:
        say("  前端    %s" % APP_URL)
    if want_backend:
        say("  后端API http://127.0.0.1:%d/docs" % BACKEND_PORT)
        say("  健康检查 %s" % HEALTH_URL)
    say("========================================")
    say("关闭对应终端窗口即可停止服务。")
    say()

    if want_frontend and not args.no_browser and not args.no_window:
        webbrowser.open(APP_URL)

    if args.no_window:
        # 前台模式：本进程即“看门狗”，Ctrl+C 时一并结束子进程。
        say("前台运行中，按 Ctrl+C 停止全部服务。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
            say()
            say("已停止。")
        return 0

    try:
        input("按回车关闭本窗口（服务继续在各自窗口运行）...")
    except EOFError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
