@echo off
echo Stopping all bot processes...
taskkill /F /IM python.exe /T 2>nul
if %errorlevel%==0 (
    echo All Python processes stopped.
) else (
    echo No Python processes were running.
)
pause
