@echo off
setlocal
cd /d "%~dp0"
if defined CI (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode demo -Workflow train -NoOpen
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode demo -Workflow train
)
if errorlevel 1 (
  echo.
  echo Demo validation failed.
  if not defined CI pause
  exit /b 1
)
endlocal
