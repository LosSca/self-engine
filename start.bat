@echo off
REM Self-Reference Engine launcher (ASCII only on purpose).
REM cmd.exe decodes .bat files with the OEM code page, so Chinese text inside a
REM batch file is corrupted on GBK consoles and once corrupted the batch parser
REM mis-splits lines and fails with bogus 'path not found' / 'defined is not recognized'.
REM All Chinese output therefore lives in launch.py, which prints UTF-8.
setlocal
title Self-Reference Engine - Launcher
cd /d "%~dp0"
chcp 65001 >nul

set "PY="
if exist "%~dp0backend\.venv\Scripts\python.exe" set "PY=%~dp0backend\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0backend\venv\Scripts\python.exe" set "PY=%~dp0backend\venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ERROR] Python interpreter not found and backend\.venv is missing.
    echo         Run install.bat first, or create the venv manually:
    echo         cd backend ^&^& python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

"%PY%" "%~dp0launch.py" %*
if errorlevel 1 pause
endlocal
