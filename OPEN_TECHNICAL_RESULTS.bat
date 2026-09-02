@echo off
setlocal
cd /d "%~dp0"
if not exist "output\pdf\Technical_Product_Report.pdf" (
  echo No technical results were found. Run TRAIN_AND_SCORE.bat first.
  if not defined CI pause
  exit /b 1
)
start "Bloomberg Technical Product PDF" "output\pdf\Technical_Product_Report.pdf"
if exist "dist\technical_signal_dashboard.html" start "Bloomberg Technical Dashboard" "dist\technical_signal_dashboard.html"
endlocal
