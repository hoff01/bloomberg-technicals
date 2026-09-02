@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\install_kronos_windows.ps1"
if errorlevel 1 (
  echo.
  echo Optional Kronos installation failed. The base technical system is unchanged.
  pause
  exit /b 1
)
endlocal
