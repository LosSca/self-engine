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

# --- Optional: Neo4j via Docker --------------------------------------------
# Neo4j is only needed by the graph / knowledge-base features. The engine, the
# API and the web UI start fine without it, so this step never aborts setup.
echo "[1/5] Neo4j via Docker (OPTIONAL - safe to skip)"
DOCKER_COMPOSE=""
if ! command -v docker >/dev/null 2>&1; then
  echo "      Docker not found - skipping. Install Docker if you need the graph features later."
elif ! docker info >/dev/null 2>&1; then
  echo "      Docker is installed but the daemon is not running - skipping."
elif docker compose version >/dev/null 2>&1; then
  DOCKER_COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DOCKER_COMPOSE="docker-compose"
else
  echo "      docker compose plugin not found - skipping Neo4j."
fi

if [ -n "$DOCKER_COMPOSE" ]; then
  if $DOCKER_COMPOSE up -d neo4j; then
    echo "      Waiting for Neo4j to start..."
    sleep 15
  else
    echo "      WARNING: could not start Neo4j - continuing without it."
  fi
fi

echo
echo "[2/5] Setting up Python virtual environment (backend/.venv)..."
if [ ! -x backend/.venv/bin/python ]; then
  "$PYTHON" -m venv backend/.venv
fi
# shellcheck disable=SC1091
. backend/.venv/bin/activate
pip install --upgrade pip
pip install -r backend/requirements.txt
deactivate

echo
echo "[3/5] Copying environment file..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "      Created .env - please add your API keys before first use!"
else
  echo "      .env already exists, skipping."
fi

echo
echo "[4/5] Installing frontend dependencies..."
if [ -f frontend/package-lock.json ]; then
  ( cd frontend && npm ci )
else
  ( cd frontend && npm install )
fi

echo
echo "[5/5] Setup complete!"
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
