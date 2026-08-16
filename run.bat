@echo off
REM Double-click this file to start Pivot Desk on Windows.
cd /d "%~dp0"
title Pivot Desk

echo.
echo   Pivot Desk - starting up
echo   ------------------------
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo   Python is not installed, or not on your PATH.
    echo   Install it from https://www.python.org/downloads/
    echo   During install, tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

if not exist "venv\" (
    echo   First run - creating a private Python environment...
    python -m venv venv
    if errorlevel 1 (
        echo   Could not create the environment. See the message above.
        pause
        exit /b 1
    )
)

echo   Checking packages...
call "venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
call "venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo.
    echo   Package install failed. Scroll up for the reason.
    pause
    exit /b 1
)

echo.
echo   Ready. Opening http://localhost:8000
echo   Leave this window open. Press Ctrl+C here to stop.
echo.

start "" http://localhost:8000
call "venv\Scripts\python.exe" app.py

echo.
echo   Pivot Desk stopped.
pause
