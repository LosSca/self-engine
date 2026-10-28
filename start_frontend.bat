@echo off
chcp 65001 >nul
title 自指引擎 - 前端
cd /d "%~dp0frontend"
echo 自指引擎前端 @ http://localhost:5173  (Ctrl+C 停止)
npm run dev
pause
