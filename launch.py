"""自指引擎统一启动器 / Self-Reference Engine launcher.

本文件是项目唯一的启动编排器：Windows 入口是 start.bat，Linux/macOS 入口是
scripts/start.sh，两者都只做纯 ASCII 的解释器探测，然后把命令行参数原样透传
给这里。旧版的 start_backend.bat / start_frontend.bat / start_debug.bat /
start-mobile.bat 已删除，它们的能力改由本文件的参数承接（见 --help）。

两条历史教训，改动本文件时请保留：

1) .bat 一律只写 ASCII。cmd.exe 用 OEM 代码页解码批处理文件，.bat 里的 UTF-8
   中文会被读坏，坏了之后批处理解析器还会错切行，报出 “系统找不到指定的路径”
   之类与真实原因无关的假错误。所有中文提示都只在 Python 里输出（本文件把
   stdout/stderr 重设为 UTF-8）。
2) 窗口模式不使用 "cmd /c <含引号的长命令>"。Windows 的 argv 转义会把内部引号
   变成 \"，cmd.exe 会把它当成字面反斜杠引号而报“不是内部或外部命令”。改为
   用本文件自我重启（python launch.py --run-backend），参数以列表传给
   CreateProcess，完全绕开 cmd.exe 的引号解析。

用法：
    python launch.py                  # 本地开发：后端 8000 + 前端 5173，并打开浏览器
    python launch.py --mobile         # 手机/局域网访问（等价 --host 0.0.0.0）
    python launch.py --prod           # 生产构建 + 自带静态服务器托管（页面真的能渲染）
    python launch.py --debug          # 后端 --reload + debug 日志，并转储 LLM 调用
    python launch.py --backend-only   # 只起后端（旧的 start_backend.bat）
    python launch.py --frontend-only  # 只起前端（旧的 start_frontend.bat）
    python launch.py --no-browser     # 不自动打开浏览器
    python launch.py --no-window      # 不开新窗口，日志写 .logs/，Ctrl+C 停全部（自检/CI）

详细说明见 docs/launch.md。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
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
DIST = FRONTEND / "dist"
LOGS = ROOT / ".logs"

# 端口可用环境变量覆盖：桌面壳（desktop/）会挑一个空闲端口，再通过 LUNAR_BACKEND_PORT 传进来。
BACKEND_PORT = int(os.environ.get("LUNAR_BACKEND_PORT") or 8000)
FRONTEND_PORT = 5173
PROD_PORT = 4173
SERVE_DIST = ROOT / "scripts" / "serve-dist.mjs"
DEFAULT_HOST = "127.0.0.1"
WILDCARD_HOSTS = {"0.0.0.0", "::", ""}
HEALTH_URL = "http://127.0.0.1:%d/api/health" % BACKEND_PORT

CREATE_NEW_CONSOLE = 0x00000010

LABELS = {"backend": "后端", "frontend": "前端"}
LOG_NAMES = {"backend": "backend.log", "frontend": "frontend.log"}

BANNER = """
========================================
  自指引擎 Self-Reference Engine
  规则决定事实 · 事件记录变化 · 状态保存结果 · 模型只负责叙事
========================================
"""

EPILOG = """三种典型用法：
  本地开发      python launch.py                （或双击 start.bat）
  手机/局域网   python launch.py --mobile       （等价 --host 0.0.0.0，会打印局域网地址）
  预览构建产物  python launch.py --prod          （自动 npm run build；加 --rebuild 强制重建）

自检/CI 用 --no-window：不开新窗口、日志写 .logs/、Ctrl+C 一并停掉子进程。

知识图谱（可选）：python launch.py --neo4j status|start|stop —— 没有 Neo4j 也能完整游玩，
它只是实体关系图谱那一层镜像。
"""


def say(msg: str = "") -> None:
    print(msg, flush=True)


def check_host(value: str) -> str:
    """只接受 IP 或主机名：前端命令要经 cmd.exe 解析，参数里不能出现引号/空格。"""
    host = (value or "").strip()
    if not host:
        raise argparse.ArgumentTypeError("--host 不能为空：本机用 127.0.0.1，局域网/手机用 0.0.0.0")
    if not re.fullmatch(r"[0-9A-Za-z._:\-]+", host):
        raise argparse.ArgumentTypeError("--host 只接受 IP 或主机名（不能含空格、引号、斜杠）：%r" % value)
    return host


def display_host(host: str) -> str:
    """通配地址不能直接拿去拼 URL，浏览器里请用回环地址。"""
    return DEFAULT_HOST if host in WILDCARD_HOSTS else host


def frontend_url(host: str, prod: bool = False, port: int | None = None) -> str:
    return "http://%s:%d" % (display_host(host), port or frontend_port(prod))


def frontend_port(prod: bool) -> int:
    """--prod 由自带静态服务器托管（4173），开发模式才是 vite dev 的 5173。"""
    return PROD_PORT if prod else FRONTEND_PORT


def free_frontend_port(preferred: int, attempts: int = 20) -> int | None:
    """从 preferred 起找一个空闲端口（5173 → 5174 → 5175 …）。

    为什么要顺延而不是失败：开发机上 5173 常被别的项目长期占着（本机就是这样，
    一个 three.js 游戏常驻 5173）。旧行为是「端口有人 ⇒ 认为前端已在运行」，
    于是启动器把浏览器指到别人的页面上，用户看到的就成了“双击没启动、只打开个网页”。
    """
    for offset in range(attempts):
        candidate = preferred + offset
        if not port_open(candidate):
            return candidate
    return None


def is_our_page(port: int) -> bool:
    """端口上跑的是不是本项目的页面（而不是别人的站点）。

    判据只用最小事实：首页里有我们的挂载根节点 #root，且挂的是 /src/main.jsx
    （vite dev）或带项目标识（serve-dist 托管的构建产物）。端口没人 → False；
    是别人的页面 → False，调用方据此改用顺延端口，绝不把用户带去陌生站点。
    """
    if not port_open(port):
        return False
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2.0) as resp:
            body = resp.read(8000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    lowered = body.lower()
    if 'id="root"' not in lowered:
        return False
    return "/src/main.jsx" in lowered or "自指引擎" in body or "self-reference" in lowered


def is_lan_ipv4(ip: str) -> bool:
    """是否为常见私网段：192.168./10./172.16-31.（与手机同网段才连得上）。"""
    if ip.startswith("127."):
        return False
    if ip.startswith("192.168.") or ip.startswith("10."):
        return True
    parts = ip.split(".")
    if len(parts) == 4 and parts[0] == "172" and parts[1].isdigit():
        return 16 <= int(parts[1]) <= 31
    return False


def lan_ipv4_addresses() -> list[str]:
    """枚举本机局域网 IPv4 地址，用于 --mobile 打印手机可访问的地址。"""
    found: list[str] = []

    # 主出口地址：UDP connect 不会真的发包，只是让系统选出到默认路由的那张网卡。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            found.append(sock.getsockname()[0])
    except OSError:
        pass

    # 兜底：主机名解析，能捡到第二张网卡（虚拟网卡、第二个 WiFi）。
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    unique: list[str] = []
    for ip in found:
        if is_lan_ipv4(ip) and ip not in unique:
            unique.append(ip)
    return unique


def find_python() -> str | None:
    # backend 的虚拟环境是 backend/.venv（旧的 install 脚本曾创建 backend/venv，
    # 两种都认，避免老环境打不开）。
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python"):
        candidate = BACKEND / rel
        if candidate.exists():
            return str(candidate)
    if sys.executable:
        return sys.executable
    return None


def port_open(port: int, host: str = DEFAULT_HOST) -> bool:
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


def backend_command(py: str, host: str, debug: bool) -> list[str]:
    level = "debug" if debug else "info"
    cmd = [py, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(BACKEND_PORT), "--log-level", level]
    if debug:
        cmd.insert(3, "--reload")
    return cmd


def npm_command(args: list[str]) -> list[str]:
    """npm 在 Windows 上是 npm.cmd，CreateProcess 不能直接执行 .cmd。

    统一交给 cmd 解析——此处参数里没有引号，也就没有第 2 条教训里的转义陷阱。
    """
    if os.name == "nt":
        return ["cmd", "/c", "npm", *args]
    return ["npm", *args]


def node_exe() -> str:
    return shutil.which("node") or "node"


def frontend_command(host: str, prod: bool, port: int | None = None) -> list[str]:
    """开发模式跑 vite dev；--prod 用 scripts/serve-dist.mjs 托管 frontend/dist。

    为什么 --prod 不用 vite preview：preview 是一串中间件，任何一处插件顺序变化都可能
    把 dist 里的 index.html 原样吐回去（本机沙箱就遇到过：预览页把源文件的
    /src/main.jsx 又带出来，浏览器拿到 index.html 当模块执行 → 白屏）。
    自带的小静态服务器只做三件事：找文件、SPA 回退、把 /api 与 /media 反代到后端，
    行为可读可测。

    --strictPort 只对 vite dev 有意义：端口被占用时直接失败，避免“等就绪”等到的其实
    是别人的进程。
    """
    if prod:
        return [
            node_exe(),
            str(SERVE_DIST),
            str(DIST),
            str(PROD_PORT),
            "http://%s:%d" % (display_host(host), BACKEND_PORT),
        ]
    return npm_command(["run", "dev", "--", "--host", host, "--port", str(port or FRONTEND_PORT)])

def service_command(
    kind: str, py: str, host: str, prod: bool, debug: bool, port: int | None = None
) -> tuple[list[str], Path]:
    """单个服务的启动命令与工作目录。"""
    if kind == "backend":
        return backend_command(py, host, debug), BACKEND
    return frontend_command(host, prod, port), FRONTEND


def writable_dir(path: Path) -> bool:
    """目录存在不等于能写：本机 .logs/ 的 ACL 曾只给 Users 读权限，
    mkdir(exist_ok=True) 会“成功”，随后 open('ab') 才炸。这里先真的试写一次。"""
    probe = path / ".write-probe"
    try:
        path.mkdir(parents=True, exist_ok=True)
        with open(probe, "ab"):
            pass
        probe.unlink()
        return True
    except OSError:
        return False


def _temp_dir() -> Path:
    """系统临时目录；某些受限环境里 tempfile 一个候选目录都探不到，此时退到仓库内。"""
    try:
        return Path(tempfile.gettempdir())
    except OSError:
        return ROOT / ".tmp"


def log_path(kind: str) -> Path:
    """日志文件位置：.logs/ → 临时目录/lunar-logs → 仓库根目录。

    最后一级必须真的能创建，否则 open() 在服务启动时就抛错，看起来完全像「服务起不来」。
    日志放仓库根目录（.gitignore 已覆盖 *.log）总比启动失败好。
    """
    candidates = (LOGS, _temp_dir() / "lunar-logs", ROOT)
    for directory in candidates:
        if writable_dir(directory):
            return directory / LOG_NAMES[kind]
    return Path(tempfile.mkdtemp(prefix="lunar-")) / LOG_NAMES[kind]


def spawn_service(
    kind: str, py: str, host: str, prod: bool, debug: bool, no_window: bool, port: int | None = None
) -> subprocess.Popen:
    """启动一个服务进程。默认开独立窗口（关闭窗口即停止该服务），--no-window 时写日志。"""
    label = LABELS[kind]
    env = child_env(debug)
    if no_window:
        cmd, cwd = service_command(kind, py, host, prod, debug, port)
        path = log_path(kind)
        handle = open(path, "ab", buffering=0)  # noqa: SIM115 - 生命周期交给子进程
        say("  · %s -> %s" % (label, path))
        return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL)

    # 自我重启：参数以列表传进去，不经过 cmd.exe。
    relaunch = [sys.executable, str(Path(__file__).resolve()), "--run-" + kind, "--host", host]
    if port:
        relaunch += ["--port", str(port)]
    if prod:
        relaunch.append("--prod")
    if debug:
        relaunch.append("--debug")
    say("  · %s -> 独立窗口「自指引擎 - %s」" % (label, label))
    return subprocess.Popen(relaunch, cwd=str(ROOT), env=env, creationflags=CREATE_NEW_CONSOLE)


def stop_children(children: list[subprocess.Popen]) -> None:
    """停掉本进程启动的子进程（--no-window 的看门狗与失败回滚都用它）。

    Windows 上前端是 cmd.exe -> npm -> node 的进程树，只 terminate cmd 会留下
    vite 继续占着 5173，所以按 PID 做 taskkill /T（只杀本进程创建的这棵树，
    不会碰到用户自己开的窗口）。
    """
    live = [child for child in children if child.poll() is None]
    for child in live:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(child.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                child.terminate()
        except OSError:
            pass
    for child in live:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except OSError:
                pass


def tail_log(path: Path, max_lines: int = 12) -> list[str]:
    """失败时把子进程日志的最后几行打出来——否则 --no-window 下用户只看到“超时”。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return lines[-max_lines:]


def run_service(kind: str, host: str, prod: bool, debug: bool, port: int | None = None) -> int:
    """在独立控制台窗口内实际运行一个服务，退出后停住窗口以便查看报错。"""
    label = LABELS[kind]
    try:  # 只在真正的控制台里有效；重定向时会抛异常，忽略即可。
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW("自指引擎 - %s" % label)
    except Exception:  # noqa: BLE001 - 标题设置失败不应阻断启动
        pass

    py = find_python() or sys.executable
    cmd, cwd = service_command(kind, py, host, prod, debug, port)
    say("=" * 44)
    say(" 自指引擎 · %s" % label)
    say("=" * 44)
    say("命令: %s" % " ".join(cmd))
    say("目录: %s" % cwd)
    say("")
    code = subprocess.call(cmd, cwd=str(cwd), env=child_env(debug))
    say("")
    say("[%s 已退出，退出码 %s]" % (label, code))
    say("关闭本窗口不影响其他服务；重新启动请再次运行 start.bat（或 ./scripts/start.sh）。")
    try:
        input("按回车关闭本窗口...")
    except EOFError:
        pass
    return code


def python_spec(py: str) -> str:
    """打印给用户看/复制的解释器路径：在项目内就用相对路径，换台机器也能照抄。"""
    try:
        rel = Path(py).resolve().relative_to(ROOT)
        text = str(rel).replace("/", "\\")
        return text if os.name == "nt" else str(rel)
    except (ValueError, OSError):
        return py


def print_failure_help(what: str, kind: str | None = None, show_log: bool = False) -> None:
    say()
    say("[失败] %s 没有在预期时间内就绪。" % what)
    if show_log and kind:
        # 子进程多半已经带着真实原因退出了（端口冲突、依赖缺失、spawn EPERM…），
        # 不给日志的话用户只能看到“超时”这一句，完全无从排查。
        path = log_path(kind)
        tail = tail_log(path)
        if tail:
            say("  · %s 的最后几行输出（%s）：" % (LABELS[kind], path))
            for line in tail:
                say("      %s" % line)
    py = find_python() or "python"
    if not (FRONTEND / "node_modules").is_dir():
        say("  · frontend/node_modules 不存在：先执行  cd frontend && npm install")
    say("  · 查看对应窗口里的报错；--no-window 模式见 .logs/backend.log / .logs/frontend.log")
    say("  · 依赖缺失时执行  %s -m pip install -r backend/requirements.txt" % python_spec(py))
    say("  · 端口被占用时用  netstat -ano | findstr :8000  （:5173 同理）排查")
    say("  · 常见问题见 docs/launch.md")


def build_frontend() -> bool:
    """跑一次 `npm run build`，把 frontend/dist 刷新成当前源码。

    为什么要自动构建：--prod 托管的是 dist 里的旧文件，源码改了但没重新 build 时，
    浏览器看到的是过期界面（最容易表现为“改了半天没反应”）。构建只需几秒，
    放在启动路径里比让用户记着敲 npm run build 更可靠。
    """
    say("[构建] frontend/dist 需要刷新，正在执行 npm run build …")
    try:
        subprocess.run(
            npm_command(["run", "build"]),
            cwd=str(FRONTEND),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        say("[错误] 前端构建失败：%s" % exc)
        print_failure_help("前端")
        return False
    return True


def preflight_prod(rebuild: bool = False) -> bool:
    """--prod 需要已构建好的 frontend/dist；缺失或要求重建时先跑一次 npm run build。"""
    has_dist = (DIST / "index.html").is_file()
    if not has_dist:
        say("[提示] 没有找到 frontend/dist/index.html，先构建前端。")
    if rebuild or not has_dist:
        if not build_frontend():
            return False
    if (DIST / "index.html").is_file():
        return True
    say("[错误] 构建结束仍没有 frontend/dist/index.html，请手动执行：")
    say("         cd frontend && npm run build")
    return False

def run_neo4j_tool(action: str) -> int:
    """把 --neo4j 转交给 scripts/neo4j_tool.py，用同一个解释器（需要 neo4j 驱动）。"""
    import subprocess
    import sys as _sys

    script = ROOT / "scripts" / "neo4j_tool.py"
    if not script.is_file():
        say("[错误] 找不到 %s" % script)
        return 1
    return subprocess.call([_sys.executable, str(script), action])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="launch.py",
        description="自指引擎统一启动器：一条命令拉起后端与前端，并等待 /api/health 就绪。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        type=check_host,
        metavar="地址",
        help="绑定地址：默认 127.0.0.1（只有本机能访问）；填 0.0.0.0 允许手机等局域网设备访问，"
        "并会打印可用的局域网地址。",
    )
    parser.add_argument(
        "--mobile",
        action="store_true",
        help="手机/局域网访问的快捷方式，等价于 --host 0.0.0.0（取代已删除的 start-mobile.bat）。",
    )
    parser.add_argument(
        "--prod",
        action="store_true",
        help="生产构建模式：先 npm run build，再用 scripts/serve-dist.mjs 托管 frontend/dist"
        "（不再热更新，端口 4173）。默认仍是 vite dev。",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="--prod 启动前先重新执行 npm run build（默认只在 dist 缺失时自动构建）。",
    )
    parser.add_argument("--backend-only", action="store_true", help="只启动后端（8000），不起前端。")
    parser.add_argument("--frontend-only", action="store_true", help="只启动前端（5173），假定后端已就绪。")
    parser.add_argument(
        "--debug", action="store_true", help="后端开 --reload 与 debug 日志，并设置 LUNAR_DUMP_LLM_CALLS=1 转储 LLM 调用。"
    )
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器。")
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="不开新窗口：两个服务放在本进程前台，日志写 .logs/，Ctrl+C 一并停止（自检/CI/只用一个终端时用）。",
    )
    parser.add_argument(
        "--neo4j",
        nargs="?",
        const="status",
        default=None,
        metavar="动作",
        help="管理可选的 Neo4j 图谱库：status（默认）/ start / stop / password <新密码>。"
        "等同 python scripts/neo4j_tool.py <动作>，不启动后端与前端。",
    )
    parser.add_argument("--run-backend", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-frontend", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # --mobile 只是 --host 0.0.0.0 的别名；显式给了 --host 时以 --host 为准。
    host = args.host
    if args.mobile:
        if args.host == DEFAULT_HOST:
            host = "0.0.0.0"
        else:
            say("[提示] 同时给了 --mobile 与 --host %s，按 --host 处理。" % args.host)

    # 图谱库小工具：只转交，不拉起任何服务。
    if args.neo4j:
        return run_neo4j_tool(args.neo4j)

    # 内部模式：本进程就是这个服务（由窗口模式自我重启产生）。
    if args.run_backend:
        return run_service("backend", host, args.prod, args.debug)
    if args.run_frontend:
        return run_service("frontend", host, args.prod, args.debug, args.port)

    say(BANNER)

    py = find_python()
    if not py:
        say("[错误] 找不到 Python 解释器，请先运行 install.bat（Windows）或 ./install.sh（Linux/macOS）。")
        return 1
    say("[环境] 解释器: %s" % py)
    say("[环境] 后端目录: %s" % BACKEND)
    say("[环境] 前端目录: %s" % FRONTEND)
    say("[环境] 模式: %s" % ("预览构建产物" if args.prod else "开发服务器"))
    say("[环境] 绑定地址: %s" % host)

    want_backend = not args.frontend_only
    want_frontend = not args.backend_only

    # ---- 预检：先判定清楚再启动任何进程，避免半途失败留下孤儿进程 ----
    if want_frontend:
        if args.prod and not preflight_prod(rebuild=args.rebuild):
            return 1
        if not (FRONTEND / "node_modules").is_dir() and not args.prod:
            say("[错误] frontend/node_modules 不存在，请先执行  cd frontend && npm install")
            print_failure_help("前端")
            return 1
        if args.prod and not shutil.which("node"):
            say("[错误] --prod 需要 node 来托管 frontend/dist，但 PATH 里找不到 node。")
            return 1
    if want_backend and port_open(BACKEND_PORT) and not http_ok(HEALTH_URL):
        say("[错误] 端口 %d 已被其他进程占用，但它不是本项目的后端。" % BACKEND_PORT)
        say("       请关闭占用进程后重试：netstat -ano | findstr :%d" % BACKEND_PORT)
        print_failure_help("后端")
        return 1

    children: list[subprocess.Popen] = []
    started_backend = False
    started_frontend = False
    # 前端实际使用的端口：被别的程序占用时顺延。--prod 也允许顺延（4173 → 4174 …）。
    fe_port: int | None = None

    # ---- 后端 ----
    if want_backend:
        if http_ok(HEALTH_URL):
            say("[1/3] 后端已在运行: %s" % HEALTH_URL)
        else:
            say("[1/3] 启动后端: %s（host=%s）" % (HEALTH_URL, host))
            children.append(spawn_service("backend", py, host, args.prod, args.debug, args.no_window))
            started_backend = True
    else:
        say("[1/3] 跳过后端（--frontend-only）")

    # ---- 前端 ----
    if want_frontend:
        preferred = args.port or frontend_port(args.prod)
        if is_our_page(preferred):
            fe_port = preferred
            say("[2/3] 前端已在运行: %s" % frontend_url(host, args.prod, fe_port))
        else:
            if port_open(preferred):
                # 端口被人占着但不是本项目：绝不把浏览器指过去，换成顺延端口。
                say("[2/3] 端口 %d 已被其他程序占用（不是本项目页面），改用下一个空闲端口" % preferred)
            picked = free_frontend_port(preferred)
            if picked is None:
                say("[错误] 从 %d 起连续 20 个端口都被占用，无法启动前端。" % preferred)
                return 1
            fe_port = picked
            say(
                "[2/3] 启动前端: %s（%s，host=%s）"
                % (frontend_url(host, args.prod, fe_port), "serve-dist" if args.prod else "vite dev", host)
            )
            children.append(
                spawn_service("frontend", py, host, args.prod, args.debug, args.no_window, fe_port)
            )
            started_frontend = True
    else:
        say("[2/3] 跳过前端（--backend-only）")

    # ---- 等待就绪 ----
    ok = True
    if started_backend:
        say("[3/3] 等待后端就绪")
        if not wait_until(lambda: http_ok(HEALTH_URL), 120):
            print_failure_help("后端", "backend", show_log=args.no_window)
            ok = False
    if started_frontend and ok:
        say("[3/3] 等待前端就绪")
        if not wait_until(lambda: is_our_page(fe_port or frontend_port(args.prod)), 120):
            print_failure_help("前端", "frontend", show_log=args.no_window)
            ok = False

    if not ok:
        say()
        say("服务未能全部就绪，请按上面的提示排查。")
        if args.no_window:
            stop_children(children)
            say("已回滚本次启动的进程。")
        return 1

    say()
    say("========================================")
    say("  已就绪：")
    if want_frontend:
        say("  前端    %s" % frontend_url(host, args.prod, fe_port))
    if want_backend:
        say("  后端API http://%s:%d/docs" % (display_host(host), BACKEND_PORT))
        say("  健康检查 %s" % HEALTH_URL)
    if want_frontend and args.prod:
        say("  说明    --prod 托管的是 frontend/dist 的构建产物；改源码后重跑本命令（或加 --rebuild）即可刷新")

    if host not in WILDCARD_HOSTS and host != DEFAULT_HOST:
        say(
            "  局域网  前端 http://%s:%d  后端 http://%s:%d/docs"
            % (host, fe_port or frontend_port(args.prod), host, BACKEND_PORT)
        )
    elif host in WILDCARD_HOSTS:
        addrs = lan_ipv4_addresses()
        say("  局域网/手机访问（同一 WiFi 下）：")
        if addrs:
            for ip in addrs:
                say("    前端    http://%s:%d" % (ip, fe_port or frontend_port(args.prod)))
            say("    后端API http://%s:%d/docs" % (addrs[0], BACKEND_PORT))
            say("    手机浏览器打开上面的“前端”地址即可；首次访问若被拦截，")
            say("    请在 Windows 防火墙里允许 python.exe / node.exe 访问“专用网络”。")
        else:
            say("    没有找到 192.168./10./172.16-31 网段的 IPv4 地址，可能是没连局域网。")
            say("    可先执行 ipconfig 确认本机地址，再用 --host 0.0.0.0 重试。")
    if want_frontend and fe_port is not None and fe_port != frontend_port(args.prod):
        say("  说明    默认端口 %d 被其他程序占用，本次前端跑在 %d" % (frontend_port(args.prod), fe_port))
    say("========================================")
    say("关闭对应终端窗口即可停止服务。")
    say()

    if want_frontend and not args.no_browser and not args.no_window:
        webbrowser.open(frontend_url(host, args.prod, fe_port))

    if args.no_window:
        # 前台模式：本进程即“看门狗”，Ctrl+C 时一并结束子进程。
        say("前台运行中，按 Ctrl+C 停止全部服务。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop_children(children)
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
