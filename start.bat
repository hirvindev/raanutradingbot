@echo off
REM ============================================
REM   RaanuTradingBot — one-click starter
REM ============================================

cd /d "%~dp0"

echo.
echo ============================================
echo   RaanuTradingBot - Trade 212 Bot
echo ============================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed.
    echo Install it from https://python.org/downloads
    echo Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

REM First-time setup: install dependencies if not already there
python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo First-time setup: installing Python packages...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Package installation failed.
        pause
        exit /b 1
    )
    echo.
)

REM Open the dashboard in default browser after a short delay
start "" /min cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000"

REM Run the server
python server.py

pause
