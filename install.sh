#!/bin/bash
# Project Lunar - local setup (Linux / macOS)
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$ROOT"

echo "=== Project Lunar - Local Setup ==="
echo

# --- Required: Python -------------------------------------------------------
if command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON="python"
else
  echo "ERROR: Python not found. Install from https://www.python.org/downloads/"
  exit 1
fi

# --- Required: Node.js ------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js not found. Install from https://nodejs.org/"
  exit 1
fi

echo
echo "[1/4] Setting up Python virtual environment (backend/.venv)..."
if [ ! -x backend/.venv/bin/python ]; then
  "$PYTHON" -m venv backend/.venv
fi
# shellcheck disable=SC1091
. backend/.venv/bin/activate
pip install --upgrade pip
pip install -r backend/requirements.txt
deactivate

echo
echo "[2/4] Copying environment file..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "      Created .env - please add your API keys before first use!"
else
  echo "      .env already exists, skipping."
fi

echo
echo "[3/4] Installing frontend dependencies..."
if [ -f frontend/package-lock.json ]; then
  ( cd frontend && npm ci )
else
  ( cd frontend && npm install )
fi

echo
echo "[4/4] Setup complete!"
echo
echo "========================================"
echo " Start everything with ONE command:"
echo
echo "     ./scripts/start.sh"
echo
echo " Add a parameter if you need it, e.g."
echo "     ./scripts/start.sh --debug     (auto reload + LLM call dump)"
echo "     ./scripts/start.sh --mobile    (phone / LAN access on 0.0.0.0)"
echo "     ./scripts/start.sh --prod      (serve the built frontend/dist)"
echo
echo " App:           http://127.0.0.1:5173"
echo " Backend API:   http://127.0.0.1:8000/docs"
echo " Health check:  http://127.0.0.1:8000/api/health"
echo " Neo4j Browser: http://localhost:7474   (only if you started Neo4j)"
echo
echo " Details and troubleshooting: docs/launch.md"
echo "========================================"
echo
