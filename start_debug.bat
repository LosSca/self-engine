@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 自指引擎 - 调试启动器
cd /d "%~dp0"

echo ========================================
echo  自指引擎 Self-Reference Engine
echo ========================================
echo.

REM ---- 1. Python 解释器：优先 backend\.venv，其次 backend\venv ----
set "VENV_PY="
if exist "%~dp0backend\.venv\Scripts\python.exe" set "VENV_PY=%~dp0backend\.venv\Scripts\python.exe"
if not defined VENV_PY if exist "%~dp0backend\venv\Scripts\python.exe" set "VENV_PY=%~dp0backend\venv\Scripts\python.exe"
if not defined VENV_PY (
    where python >nul 2>&1
    if %ERRORLEVEL% EQU 0 set "VENV_PY=python"
)
if not defined VENV_PY (
    echo [错误] 找不到 Python，也找不到 backend\.venv。
    echo        请先运行 install.bat，或手动创建虚拟环境：
    echo        cd backend ^&^& python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
echo [1/4] 使用解释器: !VENV_PY!

REM ---- 2. Neo4j（可选，失败不影响启动） ----
where docker >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    docker inspect lunar-neo4j >nul 2>&1
    if %ERRORLEVEL% NEQ 0 (
        echo [2/4] 正在启动 Neo4j 容器...
        docker-compose up -d neo4j >nul 2>&1
        if %ERRORLEVEL% NEQ 0 docker compose up -d neo4j >nul 2>&1
    ) else (
        echo [2/4] Neo4j 已在运行。
    )
) else (
    echo [2/4] 未检测到 Docker，跳过 Neo4j（知识图谱功能不可用，其余功能正常）。
)

REM ---- 3. 后端 ----
echo [3/4] 启动后端（LLM 调用转储开启）
start "自指引擎 - 后端" cmd /k "chcp 65001 >nul && cd /d "%~dp0backend" && set LUNAR_DUMP_LLM_CALLS=1 && "!VENV_PY!" -m uvicorn app.main:app --port 8000 --log-level debug"

REM ---- 4. 前端 ----
echo [4/4] 启动前端: http://localhost:5173
start "自指引擎 - 前端" cmd /k "chcp 65001 >nul && cd /d "%~dp0frontend" && npm run dev"

timeout /t 6 /nobreak >nul
start http://localhost:5173

echo.
echo ========================================
echo  已启动：
echo  前端   http://localhost:5173
echo  后端   http://localhost:8000/docs
echo ========================================
echo 关闭对应终端窗口即可停止服务。
echo.
endlocal
