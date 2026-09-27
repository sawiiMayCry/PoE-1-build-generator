@echo off
setlocal
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo Node.js 18 or newer is required to run this local app.
  echo Install Node.js from https://nodejs.org/ and then run start.bat again.
  pause
  exit /b 1
)
start "" "http://127.0.0.1:4173"
node server.js
pause
