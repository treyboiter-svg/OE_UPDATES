@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  MLB Daily Pitch Environment - V52 - OPEN LATEST
echo ============================================================
for /f "delims=" %%d in ('dir mlb_daily_outputs /b /ad /o-d') do (
    set LATEST=%%d
    goto :found
)
:found
if "%LATEST%"=="" (
    echo No run folders found under mlb_daily_outputs\. Run RUN_DAILY_MLB_DASHBOARD_V52.bat first.
    pause
    exit /b 1
)
set RUNDIR=mlb_daily_outputs\%LATEST%
if not exist "%RUNDIR%\mlb_dashboard_data_bundle.json" (
    echo Bundle missing for %RUNDIR%, building it now ...
    python build_dashboard_bundle_V52.py "%RUNDIR%"
)
start /min cmd /c "cd /d %RUNDIR% && python -m http.server 8765"
timeout /t 2 >nul
start "" "http://localhost:8765/mlb-pitch-environment-live-dashboard-V52.html"
echo Opened: %RUNDIR%
pause
endlocal
