@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo  MLB Daily Pitch Environment - V53 - DAILY RUN
echo ============================================================
python run_pipeline_V53.py --open-dashboard
if errorlevel 1 (
    echo.
    echo PIPELINE FAILED. Check pipeline_status.json and the pipeline_logs\ folder for stdout/stderr.
    pause
    exit /b 1
)
echo Done. See pipeline_status.json for the run_dir and stage results.
pause
endlocal
