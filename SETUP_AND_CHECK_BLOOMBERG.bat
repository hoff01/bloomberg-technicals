@echo off
setlocal
cd /d "%~dp0"
set "BBG_SETUP_CHAIN=1"
call INSTALL_BLOOMBERG.bat
if errorlevel 1 exit /b 1
call CHECK_BLOOMBERG_READY.bat
if errorlevel 1 exit /b 1
echo.
echo Bloomberg Python installation and live BDP, BDH, and BDIB checks passed.
echo Review dist\bloomberg_preflight.json for subscription and depth entitlement status.
if not defined CI pause
endlocal
