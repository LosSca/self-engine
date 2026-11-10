#!/usr/bin/env bash
# 自指引擎启动器（Linux / macOS）：start.bat 的等价物。
#
# 这里只做解释器探测，然后把参数原样透传给 launch.py——所有编排逻辑只有一份，
# 不存在"两个平台两套逻辑"的漂移。
#
# 用法：
#   ./scripts/start.sh                 本地开发（后端 8000 + 前端 5173）
#   ./scripts/start.sh --backend-only  只起后端
#   ./scripts/start.sh --frontend-only 只起前端
#   ./scripts/start.sh --debug         后端 --reload + debug 日志 + LLM 调用转储
#   ./scripts/start.sh --mobile        局域网/手机访问（等价 --host 0.0.0.0）
#   ./scripts/start.sh --prod          预览已构建的 frontend/dist
#   ./scripts/start.sh --no-window     两个服务都放当前终端，Ctrl+C 一起停
#   ./scripts/start.sh --help          全部参数（中文说明由 launch.py 打印）
#
# 可执行权限：Windows 的 NTFS 上没有 POSIX 权限位，chmod +x 在 Windows 上等于空操作，
# 所以本仓库可能没有记录这个文件的执行位。请用下面任意一种方式启动：
#   bash scripts/start.sh                             # 一定可行，不依赖权限位
#   chmod +x scripts/start.sh && ./scripts/start.sh   # Linux / macOS 上一次性设置
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=""
for candidate in \
  "backend/.venv/bin/python" \
  "backend/venv/bin/python" \
  "backend/.venv/Scripts/python.exe" \
  "backend/venv/Scripts/python.exe"
do
  if [ -x "$candidate" ]; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PY="$(command -v python)"
  else
    echo "[ERROR] No Python interpreter found and backend virtualenv is missing." >&2
    echo "        Run ./install.sh first, or create the venv manually:" >&2
    echo "        cd backend && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
  fi
fi

exec "$PY" "$ROOT/launch.py" "$@"
