#!/bin/bash
set -e

echo "=== Project Lunar - Local Setup ==="
echo

# --- Prerequisite checks ---
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker not found."
  echo "Please install from https://www.docker.com/products/docker-desktop/"
  exit 1
fi

# Resolve docker compose command (v2 plugin vs legacy binary)
if docker compose version >/dev/null 2>&1; then
  DOCKER_COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DOCKER_COMPOSE="docker-compose"
else
  echo "ERROR: docker compose not found."
  echo "Please install Docker Desktop or the docker compose plugin."
  exit 1
fi

# Resolve Python command (prefer python3, common on macOS/Linux)
if command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON="python"
else
  echo "ERROR: Python not found. Install from https://www.python.org/downloads/"
  exit 1
fi

if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: Node.js not found. Install from https://nodejs.org/"
  exit 1
fi

echo "[1/5] Starting Neo4j via Docker..."
$DOCKER_COMPOSE up -d neo4j
echo "      Waiting for Neo4j to start..."
sleep 15

echo "[2/5] Setting up Python virtual environment..."
cd backend
$PYTHON -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
cd ..

echo "[3/5] Copying environment file..."
if [ ! -f .env ]; then
  cp .env.example .env
  echo "      Created .env - please add your API keys!"
else
  echo "      .env already exists, skipping."
fi

echo "[4/5] Installing frontend dependencies..."
cd frontend
npm install
cd ..

echo
echo "[5/5] Setup complete!"
echo
echo "========================================"
echo " To start Project Lunar:"
echo
echo " Backend:"
echo "   cd backend"
echo "   source venv/bin/activate"
echo "   uvicorn app.main:app --reload --port 8000"
echo
echo " Frontend (new terminal):"
echo "   cd frontend"
echo "   npm run dev"
echo
echo " Neo4j Browser: http://localhost:7474"
echo " App:           http://localhost:5173"
echo "========================================"
echo
