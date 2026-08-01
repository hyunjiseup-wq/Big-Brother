@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
title Discord AutoMod Bot
cd /d "%~dp0"

set "PYTHON=%LOCALAPPDATA%\DiscordAutoMod\venv-3.13\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] Bot Python environment was not found: %PYTHON%
    echo Complete the installation before running the bot.
    pause
    exit /b 1
)

"%PYTHON%" -c "import discord, aiosqlite, httpx, dotenv" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Required packages are missing or the environment is damaged.
    echo Reinstall requirements.lock and try again.
    pause
    exit /b 2
)

"%PYTHON%" -c "import asyncio, database; asyncio.run(database.init_db())"
if errorlevel 1 (
    echo [ERROR] The database could not be initialized.
    echo Check AUTOMOD_DB_PATH and folder permissions. The detailed error is shown above.
    pause
    exit /b 4
)

"%PYTHON%" -c "import bot; bot.validate_runtime_environment()"
if errorlevel 1 (
    echo [ERROR] Required values in .env are missing or invalid.
    echo Review the detailed error above and update .env.
    pause
    exit /b 5
)

if /i "%~1"=="--check" (
    echo Environment check passed: %PYTHON%
    exit /b 0
)

if /i "%~1"=="--check-network" (
    "%PYTHON%" network_check.py
    exit /b %errorlevel%
)

set /a RETRIES=0
:loop
echo [%date% %time%] Starting bot...
"%PYTHON%" bot.py
set "BOT_EXIT=%errorlevel%"
if "%BOT_EXIT%"=="3" goto duplicate

set /a RETRIES+=1
if %RETRIES% GEQ 5 goto failed
echo [%date% %time%] Bot exited. Restarting in 10 seconds. (%RETRIES%/5)
timeout /t 10 /nobreak >nul
goto loop

:duplicate
echo [INFO] The bot is already running.
pause
exit /b 3

:failed
echo [ERROR] Bot stopped five times. Automatic restart has been disabled.
echo Review the console error and configuration.
pause
exit /b %BOT_EXIT%
