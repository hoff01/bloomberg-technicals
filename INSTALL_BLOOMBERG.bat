@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_technical_windows.ps1" -Mode live -InstallOnly -NoOpen
if errorlevel 1 (
  echo.
  echo Bloomberg environment installation failed.
  pause
  exit /b 1
)
echo.
echo Bloomberg and Python dependencies are ready.
if not defined BBG_SETUP_CHAIN if not defined CI pause
endlocal
