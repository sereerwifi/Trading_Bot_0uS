@echo off
REM Double-click this file to refresh the dashboard and open it in your browser.
REM Make sure MT5 terminal is running and logged in first.
cd /d "%~dp0"
python generate_dashboard.py
if exist dashboard.html (
    start dashboard.html
) else (
    echo Dashboard generation failed - see the message above.
    pause
)
