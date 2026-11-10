@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
REM ---------------------------------------------------------------------------
REM  Project Lunar - local setup (Windows)
REM
REM  NOTE: this file is intentionally ASCII-only. cmd.exe decodes .bat files
REM  with the OEM code page, so UTF-8 Chinese text inside a batch file gets
REM  garbled and can even make the parser split lines wrongly. All Chinese
REM  hints are printed by launch.py instead.
REM ---------------------------------------------------------------------------
cd /d "%~dp0"
echo === Project Lunar - Local Setup ===
echo.

REM --- Optional: Neo4j via Docker -------------------------------------------
REM  Neo4j is ONLY needed by the graph/knowledge-base features. The core
REM  engine, the API and the web UI run fine without it, and Docker Desktop is
REM  not installed on most laptops. So this step never aborts the setup.
echo [1/5] Neo4j via Docker (OPTIONAL - safe to skip)
docker --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo       Docker not found - skipping. If you need the graph features later,
    echo       install Docker Desktop from https://www.docker.com/products/docker-desktop/
    goto :after_neo4j
)
docker ps >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo       Docker is installed but the daemon is not running - skipping.
    echo       Start Docker Desktop and run this script again if you need Neo4j.
    goto :after_neo4j
)
set "DOCKER_COMPOSE="
docker compose version >nul 2>&1
IF %ERRORLEVEL% EQU 0 set "DOCKER_COMPOSE=docker compose"
IF NOT DEFINED DOCKER_COMPOSE (
    docker-compose --version >nul 2>&1
    IF %ERRORLEVEL% EQU 0 set "DOCKER_COMPOSE=docker-compose"
)
IF NOT DEFINED DOCKER_COMPOSE (
    echo       docker compose plugin not found - skipping Neo4j.
    goto :after_neo4j
)
echo       Starting Neo4j container...
%DOCKER_COMPOSE% up -d neo4j
IF %ERRORLEVEL% NEQ 0 (
    echo       WARNING: could not start Neo4j - continuing without it.
    echo       The engine and the web UI work without the graph database.
    goto :after_neo4j
)
echo       Waiting for Neo4j to start...
timeout /t 15 /nobreak >nul
:after_neo4j

REM --- Required: Python ------------------------------------------------------
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found. Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

REM --- Required: Node.js -----------------------------------------------------
node --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Node.js not found. Install from https://nodejs.org/
    pause
    exit /b 1
)

echo.
echo [2/5] Setting up Python virtual environment (backend.venv)...
IF NOT EXIST "backend\.venv\Scripts\python.exe" (
    python -m venv backend\.venv
)
IF NOT EXIST "backend\.venv\Scripts\python.exe" (
    echo ERROR: could not create backend\.venv
    pause
    exit /b 1
)
"backend\.venv\Scripts\python.exe" -m pip install --upgrade pip
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip is not usable inside backend\.venv
    pause
    exit /b 1
)
"backend\.venv\Scripts\python.exe" -m pip install -r backend\requirements.txt
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: failed to install backend dependencies.
    pause
    exit /b 1
)

echo.
echo [3/5] Copying environment file...
IF NOT EXIST ".env" (
    copy ".env.example" ".env" >nul
    echo       Created .env - please add your API keys before first use!
) ELSE (
    echo       .env already exists, skipping.
)

echo.
echo [4/5] Installing frontend dependencies...
pushd frontend
IF EXIST "package-lock.json" (
    call npm ci
) ELSE (
    call npm install
)
IF %ERRORLEVEL% NEQ 0 (
    popd
    echo ERROR: npm install failed in frontend\.
    pause
    exit /b 1
)
popd

echo.
echo [5/5] Setup complete!
echo.
echo ========================================
echo  Start everything with ONE command:
echo.
echo      start.bat
echo.
echo  Add a parameter if you need it, e.g.
echo      start.bat --debug          (auto reload + LLM call dump)
echo      start.bat --mobile         (phone / LAN access on 0.0.0.0)
echo      start.bat --prod           (serve the built frontend/dist)
echo.
echo  App:           http://127.0.0.1:5173
echo  Backend API:   http://127.0.0.1:8000/docs
echo  Health check:  http://127.0.0.1:8000/api/health
echo  Neo4j Browser: http://localhost:7474   (only if you started Neo4j)
echo.
echo  Details and troubleshooting: docs\launch.md
echo ========================================
echo.
pause
