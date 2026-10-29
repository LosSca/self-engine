# Starts the dev servers and steps aside.
#
# Why a scheduled task instead of start.bat: when this file is launched by a
# short-lived shell (an agent command, a remote session, a CI step that ends),
# every child it spawns is killed the moment that shell exits, and the ports go
# dark. A scheduled task owns the processes itself, so they outlive whoever
# asked for them. start.bat remains the right entry point for a human sitting at
# the machine.
#
# Usage:
#   schtasks /Create /TN LunarDevServer /TR "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File E:\shujia\scripts\start-dev.ps1" /SC ONCE /ST 23:59 /F
#   schtasks /Run    /TN LunarDevServer
#   schtasks /End    /TN LunarDevServer        # stop both servers
#
# Idempotent: a port that is already listening is left alone.

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root '.logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
$node = 'C:\Program Files\nodejs\node.exe'
$vite = Join-Path $root 'frontend\node_modules\vite\bin\vite.js'

function Test-Port([int]$Port) {
  try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect('127.0.0.1', $Port)
    $client.Close()
    return $true
  } catch {
    return $false
  }
}

if (-not (Test-Port 8000)) {
  Start-Process -FilePath $python `
    -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000','--log-level','info' `
    -WorkingDirectory (Join-Path $root 'backend') -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs 'backend.log') `
    -RedirectStandardError (Join-Path $logs 'backend.err.log')
}

# Vite is started through node with the bin script rather than 'npm run dev':
# Start-Process does not quote paths containing spaces, so asking it for
# 'C:\Program Files\nodejs\node.exe <npm-cli.js>' fails with
# "Cannot find module 'C:\Program'". node + vite.js avoids the npm shim entirely.
if (-not (Test-Port 5173)) {
  Start-Process -FilePath $node -ArgumentList $vite `
    -WorkingDirectory (Join-Path $root 'frontend') -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logs 'frontend.log') `
    -RedirectStandardError (Join-Path $logs 'frontend.err.log')
}
