@echo off
setlocal
cd /d "%~dp0"
if defined CI (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode demo -Workflow score -NoOpen
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode demo -Workflow score
)
if errorlevel 1 (
  echo.
  echo Frozen-model demo scoring failed. Run RUN_TECHNICAL_DEMO.bat first.
  if not defined CI pause
  exit /b 1
)
endlocal
