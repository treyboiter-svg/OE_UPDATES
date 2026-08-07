@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  MLB Daily Pitch Environment - V52 - DAILY RUN
echo ============================================================
python MLB_DAILY_PITCH_ENVIRONMENT_V52.py
if errorlevel 1 (
    echo.
    echo RUN FAILED. Check the newest folder under mlb_daily_outputs\ for findings.csv
    pause
    exit /b 1
)
for /f "delims=" %%d in ('dir mlb_daily_outputs /b /ad /o-d') do (
    set LATEST=%%d
    goto :found
)
:found
set RUNDIR=mlb_daily_outputs\%LATEST%
echo Building dashboard bundle for %RUNDIR% ...
python build_dashboard_bundle_V52.py "%RUNDIR%"
if errorlevel 1 (
    echo.
    echo BUNDLE BUILD FAILED. Check %RUNDIR%\mlb_dashboard_system_health.json
    pause
    exit /b 1
)
echo Starting local server and opening dashboard ...
start /min cmd /c "cd /d %RUNDIR% && python -m http.server 8765"
timeout /t 2 >nul
start "" "http://localhost:8765/mlb-pitch-environment-live-dashboard-V52.html"
echo Done. Run folder: %RUNDIR%
pause
endlocal
