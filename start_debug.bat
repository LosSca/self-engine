@echo off
REM Debug launcher: backend --reload, debug logs, LLM call dump (ASCII only).
setlocal
title Self-Reference Engine - Debug
cd /d "%~dp0"
chcp 65001 >nul
set "PY="
if exist "%~dp0backend\.venv\Scripts\python.exe" set "PY=%~dp0backend\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0backend\venv\Scripts\python.exe" set "PY=%~dp0backend\venv\Scripts\python.exe"
if not defined PY set "PY=python"
"%PY%" "%~dp0launch.py" --debug %*
if errorlevel 1 pause
endlocal
