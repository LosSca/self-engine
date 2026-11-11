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
REM
REM  Neo4j (optional graph mirror) is NOT started here: the engine, the API and
REM  the web UI run fine without it. If you want the graph features, run Neo4j
REM  yourself and set NEO4J_URI in .env.
REM ---------------------------------------------------------------------------
cd /d "%~dp0"
echo === Project Lunar - Local Setup ===
echo.

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
echo [1/4] Setting up Python virtual environment (backend.venv)...
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
echo [2/4] Copying environment file...
IF NOT EXIST ".env" (
    copy ".env.example" ".env" >nul
    echo       Created .env - please add your API keys before first use!
) ELSE (
    echo       .env already exists, skipping.
)

echo.
echo [3/4] Installing frontend dependencies...
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
echo [4/4] Setup complete!
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
