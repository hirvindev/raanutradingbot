@echo off
title RaanuTradingBot — Auto Setup
color 0A

echo.
echo ============================================
echo   RaanuTradingBot — Auto Setup
echo ============================================
echo.

set FOLDER=C:\Users\Archana Arjunraj\OneDrive\Desktop\Algo Trading
set DOWNLOADS=C:\Users\Archana Arjunraj\Downloads

cd /d "%FOLDER%"

echo [1/5] Copying RaanuTradingBot.html...
if exist "%DOWNLOADS%\RaanuTradingBot.html" (
    copy /Y "%DOWNLOADS%\RaanuTradingBot.html" "%FOLDER%\RaanuTradingBot.html"
    echo       Done.
) else (
    echo       Already in folder or not in Downloads — skipping.
)

echo [2/5] Copying server.py...
if exist "%DOWNLOADS%\server.py" (
    copy /Y "%DOWNLOADS%\server.py" "%FOLDER%\server.py"
    echo       Done.
) else (
    echo       Already in folder or not in Downloads — skipping.
)

echo [3/5] Copying trade212-algo-dashboard.html...
if exist "%DOWNLOADS%\trade212-algo-dashboard.html" (
    copy /Y "%DOWNLOADS%\trade212-algo-dashboard.html" "%FOLDER%\trade212-algo-dashboard.html"
    echo       Done.
) else (
    echo       Already in folder or not in Downloads — skipping.
)

echo [4/5] Fixing .env file (practice -> demo)...
powershell -Command "(Get-Content '%FOLDER%\.env') -replace 'T212_MODE=practice', 'T212_MODE=demo' | Set-Content '%FOLDER%\.env'"
echo       Done.

echo [5/5] Pushing to GitHub...
git add .
git commit -m "Auto-setup: updated dashboard and server"
git push
echo       Done.

echo.
echo ============================================
echo   Setup complete! Starting server...
echo ============================================
echo.
echo   Dashboard: http://localhost:8000
echo   AlgoDash:  http://localhost:8000/algo
echo.

start "Cloudflare Tunnel" cmd /k "cloudflared tunnel --url http://localhost:8000"
timeout /t 2 /nobreak >nul
python server.py

pause
