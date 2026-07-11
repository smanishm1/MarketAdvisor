@echo off
REM ===========================================================================
REM  hermes-trading - one-time setup for a fresh Windows PC.
REM  Double-click this once. It builds the Python environment and installs
REM  everything. When it finishes, use start.bat to run the app.
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo Checking for Python...
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python is not installed (or not on PATH^).
  echo   1^) Install Python 3.10 or newer from:
  echo        https://www.python.org/downloads/windows/
  echo   2^) During install, TICK the box "Add python.exe to PATH".
  echo   3^) Then run this setup.bat again.
  echo.
  pause
  exit /b 1
)

echo Creating the virtual environment (.venv)...
python -m venv .venv
if errorlevel 1 ( echo. & echo   Failed to create .venv. & pause & exit /b 1 )

echo Upgrading pip...
".venv\Scripts\python" -m pip install --upgrade pip

echo Installing the app and its dependencies (a few minutes the first time)...
".venv\Scripts\python" -m pip install -e .
if errorlevel 1 ( echo. & echo   Install failed. & pause & exit /b 1 )

if not exist ".env" (
  echo Creating .env from the template...
  copy ".env.example" ".env" >nul
)

echo.
echo   Setup complete.
echo   Double-click  start.bat  to launch the dashboard.
echo.
pause
