@echo off
title Stock Automation Launcher
cd /d "%~dp0"

echo ============================================
echo   Stock Automation — Starting Services
echo ============================================
echo.
echo [1/2] Starting Streamlit app...
start "Streamlit App" cmd /k "python -m streamlit run app.py"

timeout /t 3 /nobreak >nul

echo [2/2] Starting Scheduler...
start "Scheduler" cmd /k "python run_scheduler.py"

echo.
echo Both services started in separate windows.
echo Close those windows to stop each service.
echo.
pause
