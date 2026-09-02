@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode live -Workflow auto
if errorlevel 1 (
  echo.
  echo Technical system failed. The prior data and outputs were preserved.
  pause
  exit /b 1
)
endlocal
