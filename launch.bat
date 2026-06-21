@echo off
cd /d "C:\Users\kar.patel\wnba_minutes"
set PYTHONPATH=C:\Users\kar.patel\wnba_minutes

echo Stopping any existing Streamlit processes...
taskkill /f /im streamlit.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo Clearing cached bytecode...
if exist __pycache__ rmdir /s /q __pycache__

echo Starting WNBA Minutes Projector...
echo.
echo The app will open in your browser at http://localhost:8501
echo Keep this window open while using the app.
echo Close this window to stop the app.
echo.
"C:\Users\kar.patel\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\LocalCache\local-packages\Python313\Scripts\streamlit.exe" run "C:\Users\kar.patel\wnba_minutes\app.py" --server.headless false
pause
