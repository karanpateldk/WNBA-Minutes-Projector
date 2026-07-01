@echo off
echo Force-triggering Streamlit Cloud redeploy...
echo.

cd /d "C:\Users\kar.patel\wnba_minutes"

REM Update the deploy timestamp file — any file change forces Streamlit Cloud to rebuild
echo %DATE% %TIME% > .deploy_trigger
git add .deploy_trigger
git commit -m "Force deploy: %DATE% %TIME%" --allow-empty-message
git push origin main

echo.
echo Done. Streamlit Cloud will redeploy in 1-3 minutes.
echo Watch for the build number to update in the app footer.
pause
