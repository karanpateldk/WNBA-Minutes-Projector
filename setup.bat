@echo off
echo Installing WNBA Minutes Projector dependencies...
python -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: pip install failed. Make sure Python 3.10+ is installed and on your PATH.
    pause
    exit /b 1
)
echo.
echo Running model tests...
python test_model.py
if %ERRORLEVEL% neq 0 (
    echo Tests failed — check output above.
    pause
    exit /b 1
)
echo.
echo Setup complete! Launching app...
streamlit run app.py
