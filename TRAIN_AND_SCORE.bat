@echo off
setlocal
cd /d "%~dp0"
set "BACKFILL=%~1"
if defined BACKFILL (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode live -Workflow train -Backfill "%BACKFILL%"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode live -Workflow train
)
if errorlevel 1 (
  echo.
  echo Training failed. The prior frozen model was restored; any completed atomic data updates were retained.
  pause
  exit /b 1
)
endlocal
