@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%USERPROFILE%\Pyenvs\bbg_technical_builder\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Managed environment not found. Run TRAIN_AND_SCORE.bat first.
  if not defined CI pause
  exit /b 1
)
"%PYTHON%" "scripts\build_technical_workbook.py" --mode live
if errorlevel 1 (
  echo Workbook export failed.
  if not defined CI pause
  exit /b 1
)
echo Workbook refreshed: Technical_Trading_System.xlsx
endlocal
