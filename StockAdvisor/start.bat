@echo off
setlocal enabledelayedexpansion
title StockAdvisor

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Setup has not been run yet.
    echo Please double-click setup.bat first, then try start.bat again.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env is missing. Please run setup.bat first.
    pause
    exit /b 1
)

echo ============================================================
echo  Starting StockAdvisor
echo  - Worker process: opens in its own window (the trading logic)
echo  - Dashboard: opens in its own window, then in your browser
echo  PAPER MODE ONLY. No real money can move.
echo ============================================================
echo.

REM --- Launch the worker in its own window -------------------------
start "StockAdvisor Worker" ".venv\Scripts\python.exe" worker.py

REM --- Launch the dashboard (FastAPI/uvicorn) in its own window -----
start "StockAdvisor Dashboard" ".venv\Scripts\python.exe" -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000

REM --- Give the dashboard a moment to boot, then open the browser ---
timeout /t 4 /nobreak >nul
start "" "http://127.0.0.1:8000"

echo Both processes are starting in their own windows.
echo Close those windows (or press Ctrl+C in each) to stop StockAdvisor.
echo This window can be closed safely.
pause
