@echo off
chcp 65001 >nul
title 自指引擎 - 后端
cd /d "%~dp0backend"
set "VENV_PY="
if exist ".venv\Scripts\python.exe" set "VENV_PY=.venv\Scripts\python.exe"
if not defined VENV_PY if exist "venv\Scripts\python.exe" set "VENV_PY=venv\Scripts\python.exe"
if not defined VENV_PY set "VENV_PY=python"
echo 自指引擎后端 @ http://localhost:8000  (Ctrl+C 停止)
"%VENV_PY%" -m uvicorn app.main:app --port 8000 --log-level info
pause
