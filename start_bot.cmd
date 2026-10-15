@echo off
:: SafeRest Discord Bot Launcher (Windows)
:: Created by Aljay Leodones for SafeRest

cd /d "%~dp0"
title SafeRest Discord Bot

echo ==========================================
echo  Starting SafeRest Discord Bot
echo  Created by Aljay Leodones
echo ==========================================

:: Check if virtual environment exists
if not exist "venv" (
    echo [1/3] Creating Python virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo Error: Failed to create virtual environment. Ensure Python 3 is installed and added to PATH.
        pause
        exit /b %errorlevel%
    )
)

:: Activate virtual environment
call venv\Scripts\activate.bat

:: Ensure dependencies are installed.
:: The packages are checked locally first: contacting PyPI on every launch is
:: what used to leave this step hanging on a slow or blocked connection, and
:: nothing needs downloading once the environment is built.
echo [2/3] Checking dependencies...
python -c "import importlib.metadata as m, discord, dotenv; m.version('tzdata')" >nul 2>&1
if %errorlevel% neq 0 (
    echo       Some packages are missing - installing from requirements.txt...
    python -m pip install -r requirements.txt --disable-pip-version-check --no-input --timeout 30 --retries 2
    if %errorlevel% neq 0 (
        echo.
        echo Error: Could not install the dependencies.
        echo Check your internet connection, then run this launcher again.
        pause
        exit /b %errorlevel%
    )
) else (
    echo       All packages present - skipping download.
)

:: Launch bot
echo [3/3] Launching bot...
python main.py

:: Keep window open if bot stops
echo.
echo Bot process has stopped.
pause
