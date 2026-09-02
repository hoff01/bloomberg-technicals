@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_kronos_windows.ps1"
if errorlevel 1 (
  echo.
  echo Optional Kronos forecast failed. The base technical system is unchanged.
  pause
  exit /b 1
)
endlocal
