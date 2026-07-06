@echo off
setlocal enabledelayedexpansion
title StockAdvisor Setup

echo ============================================================
echo  StockAdvisor -- one-time setup
echo ============================================================
echo.
echo This will check for Python, create a local .venv folder, install
echo the required packages, and create your .env settings file.
echo No real money can ever move -- this is a paper-trading-only tool.
echo.

REM --- 1. Check Python is installed and is 3.10+ ----------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on your PATH.
    echo Please install Python 3.10 or newer from https://www.python.org/downloads/
    echo IMPORTANT: during install, check the box "Add python.exe to PATH".
    echo Then re-run this setup.bat.
    pause
    exit /b 1
)

python --version
echo.

REM --- 2. Create the virtual environment --------------------------------
if exist ".venv\Scripts\python.exe" (
    echo [OK] Virtual environment already exists at .venv
) else (
    echo Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
)
echo.

REM --- 3. Install requirements ------------------------------------------
echo Installing required packages (this can take a few minutes the first time)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Package installation failed. Check your internet connection and try again.
    pause
    exit /b 1
)
echo [OK] Packages installed.
echo.

REM --- 4. Create .env from the template if it doesn't exist yet ---------
if exist ".env" (
    echo [OK] .env already exists -- leaving your existing settings alone.
) else (
    copy ".env.example" ".env" >nul
    echo [OK] Created .env from the template.
    echo      Default settings: PAPER MODE ONLY, free rule-based reflection, no API keys needed.
)
echo.

if not exist "data" mkdir data
if not exist "logs" mkdir logs

echo ============================================================
echo  Setup complete.
echo  Next step: double-click start.bat to launch StockAdvisor.
echo ============================================================
pause
