@echo off
setlocal
cd /d "%~dp0"
set "EXPORT_DIRECTORY=%~1"
if "%EXPORT_DIRECTORY%"=="" set "EXPORT_DIRECTORY=%USERPROFILE%\OneDrive - Energy Transfer\Trading Analytics - Documents\General\Disty Analytics\Trade_Builder"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\run_windows.ps1" -ExportDirectory "%EXPORT_DIRECTORY%"
if errorlevel 1 pause
endlocal
