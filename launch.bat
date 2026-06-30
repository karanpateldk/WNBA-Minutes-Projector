@echo off
cd /d "C:\Users\kar.patel\wnba_minutes"
set PYTHONPATH=C:\Users\kar.patel\wnba_minutes

echo Stopping any existing Streamlit processes...
taskkill /f /im streamlit.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo Clearing cached bytecode...
if exist __pycache__ rmdir /s /q __pycache__

echo Clearing stale season and roster caches...
if exist data\season_*.json del /q data\season_*.json
if exist data\espn_roster_*.json del /q data\espn_roster_*.json
if exist data\schedule_*.json del /q data\schedule_*.json
if exist .cache_cleared_pid del /q .cache_cleared_pid

echo Starting WNBA Minutes Projector...
echo.
echo App running at http://localhost:8501
echo This window can be minimized - the app keeps running.
echo Run this file again to restart with fresh data.
echo.
"C:\Users\kar.patel\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\local-packages\Python313\Scripts\streamlit.exe" run "C:\Users\kar.patel\wnba_minutes\app.py" --server.headless true --server.fileWatcherType auto --server.port 8501 --browser.serverAddress localhost
pause
