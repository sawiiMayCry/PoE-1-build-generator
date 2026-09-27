@echo off
cd /d "%~dp0"
if defined WITCHCRAFT_PYTHON if exist "%WITCHCRAFT_PYTHON%" (
  "%WITCHCRAFT_PYTHON%" server.py
  goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py
  goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
  goto :eof
)
echo Python 3 was not found. Install Python 3.10 or newer and run start.bat again.
pause
