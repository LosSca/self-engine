@echo off
REM Backend only (ASCII only: see start.bat for why).
setlocal
title Self-Reference Engine - Backend
cd /d "%~dp0"
chcp 65001 >nul
set "PY="
if exist "%~dp0backend\.venv\Scripts\python.exe" set "PY=%~dp0backend\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0backend\venv\Scripts\python.exe" set "PY=%~dp0backend\venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" "%~dp0launch.py" --backend-only %*
if errorlevel 1 pause
endlocal
