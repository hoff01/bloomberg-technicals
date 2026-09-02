@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode live -Workflow score
if errorlevel 1 (
  echo.
  echo Current-price scoring failed. Run TRAIN_AND_SCORE.bat if no frozen model exists.
  pause
  exit /b 1
)
endlocal
