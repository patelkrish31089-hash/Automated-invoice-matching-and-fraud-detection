@echo off
REM ===================================================================
REM  AP Intelligence - one-click start for Windows
REM  Double-click this file. It installs what is missing, then opens
REM  the workbench in your browser at http://localhost:8000
REM ===================================================================
setlocal
cd /d "%~dp0"
title AP Intelligence - Invoice Matching & Fraud Detection

echo.
echo   ==========================================================
echo     AP Intelligence - Invoice Matching ^& Fraud Detection
echo   ==========================================================
echo.

REM ---- find Python -------------------------------------------------
set PY=
where py >nul 2>&1 && set PY=py
if "%PY%"=="" (where python >nul 2>&1 && set PY=python)
if "%PY%"=="" (
  echo   [X] Python was not found on this computer.
  echo.
  echo       Install it from https://www.python.org/downloads/
  echo       IMPORTANT: tick "Add Python to PATH" during setup.
  echo.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set PYVER=%%v
echo   Python %PYVER% found.

REM ---- Tesseract: needed for OCR, not a Python package --------------
where tesseract >nul 2>&1
if errorlevel 1 (
  if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" (
    set "PATH=%ProgramFiles%\Tesseract-OCR;%PATH%"
    echo   Tesseract OCR found in Program Files.
  ) else (
    echo.
    echo   [!] Tesseract OCR is not installed.
    echo       The app will start, but it cannot read invoices without it.
    echo       Install from: https://github.com/UB-Mannheim/tesseract/wiki
    echo       ^(use the default install location^)
    echo.
  )
) else (
  echo   Tesseract OCR found.
)

REM ---- Python packages ---------------------------------------------
echo.
echo   Checking Python packages (first run takes a minute)...
%PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo   [!] Package install failed. Try running this file again,
  echo       or open a terminal here and run:  %PY% -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
echo   Packages ready.

REM ---- launch ------------------------------------------------------
echo.
%PY% run.py %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo   The app exited with code %RC%. The message above explains why.
  pause
)
endlocal
