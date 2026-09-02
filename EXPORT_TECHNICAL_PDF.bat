@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%USERPROFILE%\Pyenvs\bbg_technical_builder\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Managed environment not found. Run INSTALL_BLOOMBERG.bat first.
  if not defined CI pause
  exit /b 1
)
"%PYTHON%" "scripts\build_technical_pdf.py" --mode live --output "output\pdf\Technical_Product_Report.pdf"
if errorlevel 1 (
  echo Product PDF export failed.
  if not defined CI pause
  exit /b 1
)
echo Product PDF refreshed: output\pdf\Technical_Product_Report.pdf
endlocal
