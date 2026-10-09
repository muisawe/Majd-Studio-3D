@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_v9.ps1"
if errorlevel 1 (
  echo.
  echo V9 installation failed. Read the error above.
  pause
)
