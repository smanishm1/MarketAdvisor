@echo off
REM ===========================================================================
REM  hermes-trading - launch the worker + dashboard (+ Discord bot if configured),
REM  then open the browser. A few small windows will open and must stay open while
REM  you use the app. To stop the app, close those windows.
REM ===========================================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Not set up yet. Double-click  setup.bat  first.
  echo.
  pause
  exit /b 1
)

echo Starting the trading worker...
start "hermes worker" ".venv\Scripts\python" -m hermes_trading.run

echo Starting the dashboard...
start "hermes dashboard" ".venv\Scripts\python" -m uvicorn dashboard.app:app --port 8000

REM Discord approvals bot - only if a token is set in .env (skip otherwise).
findstr /b /r "DISCORD_BOT_TOKEN=." .env >nul 2>&1
if errorlevel 1 (
  echo   Discord not configured - skipping the bot ^(set DISCORD_BOT_TOKEN in .env to enable^).
) else (
  echo Starting the Discord approvals bot...
  start "hermes discord bot" ".venv\Scripts\python" -m hermes_trading.discord_bot
)

echo Opening the dashboard in your browser...
REM brief pause so the dashboard is up before the browser opens (ping works even
REM when stdin is redirected, unlike timeout).
ping -n 5 127.0.0.1 >nul
start "" "http://localhost:8000"

echo.
echo   The app is running. Keep the new windows open.
echo   Dashboard: http://localhost:8000
echo   Close this window any time - it is not needed once the app is up.
echo.
