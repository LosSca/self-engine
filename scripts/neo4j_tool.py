"""自指引擎的 Neo4j 图谱库小工具。

Neo4j 只负责「世界实体关系图谱」这一层镜像：没有它游戏照常进行，只是少了
图谱视图与跨回合的事实检索。安装脚本不会替你起服务（避免替用户下载 JDK、
拉容器），所以这里给一条最小可用的路径：

    python scripts/neo4j_tool.py status      # 现在连得上吗，连不上差什么
    python scripts/neo4j_tool.py start       # 用 Docker 起一个本地 Neo4j 5
    python scripts/neo4j_tool.py stop
    python scripts/neo4j_tool.py password    # 把首次登录密码写进 .env

三种部署方式（按推荐顺序）：

1. Docker（最省事）：`neo4j:5`，默认映射 7474/7687，数据卷 self-engine-neo4j。
2. Neo4j Desktop / 官方 zip 解压 + `bin\\neo4j.bat console`：手动启动，
   只要 7687 端口可达，本工具的 status 一样认。
3. 不用：把 .env 的 ENGINE_GRAPH_MIRROR 设为 0，图谱相关调用全部跳过。

端口/账号都从环境变量读，和 backend/app/config.py 保持一致：
NEO4J_URI、NEO4J_USER、NEO4J_PASSWORD。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

DEFAULT_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
DEFAULT_USER = os.environ.get("NEO4J_USER", "neo4j")
DEFAULT_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
CONTAINER = "self-engine-neo4j"
IMAGE = "neo4j:5"
VOLUME = "self-engine-neo4j-data"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def say(message: str) -> None:
    print(message, flush=True)


def _driver_available() -> bool:
    try:
        import neo4j  # noqa: F401
    except ImportError:
        return False
    return True


def probe(uri: str = DEFAULT_URI, user: str = DEFAULT_USER, password: str = DEFAULT_PASSWORD) -> tuple[bool, str]:
    """返回 (可用, 说明)。不需要后端在跑，直接用驱动探一次。"""
    if not _driver_available():
        return False, "当前 Python 环境没有 neo4j 驱动：用 backend/.venv 里的 python 运行本脚本"
    from neo4j import GraphDatabase  # 同步驱动：这里只是一次性探测

    try:
        driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=3)
    except Exception as exc:  # noqa: BLE001 - 探测失败就是不可用
        return False, f"驱动初始化失败：{exc}"
    try:
        driver.verify_connectivity()
        return True, "已连接"
    except Exception as exc:  # noqa: BLE001
        return False, _explain(exc, uri, password)
    finally:
        driver.close()


def _explain(exc: Exception, uri: str, password: str) -> str:
    text = str(exc)
    if "AuthenticationRateLimit" in text or "authentication failure" in text.lower():
        return (
            "服务在跑，但账号密码不对。Neo4j 首次启动会要求改掉默认密码：\n"
            f"  1) 浏览器打开 http://localhost:7474，用 {DEFAULT_USER}/neo4j 登录并按提示改密；\n"
            "  2) 把新密码写进 .env：NEO4J_PASSWORD=<新密码>，再跑 python scripts/neo4j_tool.py status"
        )
    if "Connection refused" in text or "ServiceUnavailable" in text or "Couldn't connect" in text:
        hint = "" if password else "（NEO4J_PASSWORD 也是空的：首次启动必须先设一次密码）"
        return f"连不上 {uri}{hint}。这条端口上没有 Neo4j：\n  python scripts/neo4j_tool.py start"
    return text


def cmd_status(_args) -> int:
    say(f"Neo4j URI: {DEFAULT_URI}  用户: {DEFAULT_USER}  密码: {'已设置' if DEFAULT_PASSWORD else '未设置'}")
    ok, message = probe()
    say(("[可用] " if ok else "[不可用] ") + message)
    if ok:
        return 0
    if shutil.which("docker"):
        say("检测到 Docker：python scripts/neo4j_tool.py start 可以直接起一个。")
    else:
        say(
            "本机没有 docker 命令。可用方案：装 Neo4j Desktop，或下载 neo4j-community-5.x 压缩包解压后\n"
            "执行 bin\\neo4j.bat console（Windows）/ bin/neo4j console（macOS/Linux）。\n"
            "不想用图谱：在 .env 里设 ENGINE_GRAPH_MIRROR=0，游戏完全不受影响。"
        )
    return 1


def _docker(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)


def cmd_start(args) -> int:
    if not shutil.which("docker"):
        say("[错误] 没有 docker 命令。请用 Neo4j Desktop / 官方压缩包手动启动，或设 ENGINE_GRAPH_MIRROR=0。")
        return 1
    password = args.password or DEFAULT_PASSWORD
    if not password:
        password = "selfengine-local"
        say(f"[提示] 未提供 NEO4J_PASSWORD，用默认初始密码 {password}；启动后请改成自己的并写进 .env。")
    if password == "neo4j":
        say("[错误] neo4j 5 不允许使用默认密码 'neo4j'，请换一个。")
        return 1
    running = _docker("ps", "--format", "{{.Names}}")
    if CONTAINER in (running.stdout or "").split():
        say(f"{CONTAINER} 已在运行。")
    else:
        exists = _docker("ps", "-a", "--format", "{{.Names}}")
        if CONTAINER in (exists.stdout or "").split():
            say(f"启动已有容器 {CONTAINER} …")
            result = _docker("start", CONTAINER)
        else:
            say(f"创建容器 {CONTAINER} ({IMAGE})，数据卷 {VOLUME} …")
            result = _docker(
                "run", "-d", "--name", CONTAINER,
                "-p", "7474:7474", "-p", "7687:7687",
                "-e", f"NEO4J_AUTH={DEFAULT_USER}/{password}",
                "-v", f"{VOLUME}:/data",
                "--restart", "unless-stopped",
                IMAGE,
            )
        if result.returncode != 0:
            say("[错误] docker 启动失败：\n" + (result.stderr or result.stdout).strip())
            return result.returncode
    say("等待 Neo4j 就绪（最长 120 秒）…")
    deadline = time.time() + 120
    while time.time() < deadline:
        ok, message = probe()
        if ok:
            say("[可用] 已连接。把下列配置留在 .env 里：")
            say(f"  NEO4J_URI={DEFAULT_URI}\n  NEO4J_USER={DEFAULT_USER}\n  NEO4J_PASSWORD={password}")
            return 0
        time.sleep(3)
    say("[超时] 服务没能在 120 秒内就绪，检查：docker logs " + CONTAINER)
    return 1


def cmd_stop(_args) -> int:
    if not shutil.which("docker"):
        say("[错误] 没有 docker 命令；如果是手动启动的 Neo4j，请用它的控制台/Ctrl+C 停止。")
        return 1
    result = _docker("stop", CONTAINER)
    if result.returncode != 0:
        say((result.stderr or result.stdout).strip())
        return result.returncode
    say(f"已停止 {CONTAINER}（数据保留在卷 {VOLUME} 中）。")
    return 0


def cmd_password(args) -> int:
    password = args.password
    if not password:
        say("用法：python scripts/neo4j_tool.py password <新密码>")
        return 2
    env_path = os.path.join(ROOT, ".env")
    lines: list[str] = []
    replaced = False
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("NEO4J_PASSWORD="):
            lines[index] = f"NEO4J_PASSWORD={password}"
            replaced = True
    if not replaced:
        lines.append(f"NEO4J_PASSWORD={password}")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")
    say(f"已写入 {env_path}（NEO4J_PASSWORD）。重启后端后生效。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neo4j_tool.py", description="自指引擎的 Neo4j 图谱库小工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="检查 Neo4j 是否可用，不可用时给出下一步")
    start = sub.add_parser("start", help="用 Docker 起本地 Neo4j 5")
    start.add_argument("--password", default="", help="初始密码（默认读 NEO4J_PASSWORD）")
    sub.add_parser("stop", help="停止 Docker 容器")
    pw = sub.add_parser("password", help="把密码写进仓库根的 .env")
    pw.add_argument("password")
    args = parser.parse_args(argv)
    handlers = {"status": cmd_status, "start": cmd_start, "stop": cmd_stop, "password": cmd_password}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
