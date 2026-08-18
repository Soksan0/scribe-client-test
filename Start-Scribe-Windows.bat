@echo off
setlocal
cd /d "%~dp0"

echo.
echo Starting Scribe for local Windows testing...
echo Your datasets stay on this computer. Scribe will open in your browser at localhost.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Scribe-Windows.ps1"

if errorlevel 1 (
  echo.
  echo Scribe did not start. Please send a screenshot of this window to the project owner.
  pause
  exit /b 1
)

