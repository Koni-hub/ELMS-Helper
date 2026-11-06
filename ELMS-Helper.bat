@echo off
setlocal
title ELMS Helper
cd /d "%~dp0_system"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo First launch: setting up the Python environment...
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  if errorlevel 1 (
    echo.
    echo Setup failed. Check that Python 3.10 or newer is installed and try again.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m elms_helper %*
set "RC=%ERRORLEVEL%"

if defined ELMS_NO_PAUSE exit /b %RC%

echo.
if not "%RC%"=="0" (
  echo Exit code: %RC%
  echo Logs: "%~dp0_system\logs"
  echo.
)
pause
