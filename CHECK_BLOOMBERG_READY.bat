@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode live -PreflightOnly -NoOpen
if errorlevel 1 (
  echo.
  echo Bloomberg readiness check failed. Review the displayed security and field details.
  pause
  exit /b 1
)
echo.
echo Bloomberg BDP, BDH, and BDIB preflight passed. Review subscription PASS/WARN in the receipt.
if not defined BBG_SETUP_CHAIN if not defined CI pause
endlocal
