@echo off
setlocal
title Discord AutoMod Bot
cd /d "%~dp0"

set "PYTHON=%LOCALAPPDATA%\DiscordAutoMod\venv-3.13\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] Bot Python environment was not found: %PYTHON%
    echo Complete the installation before running the bot.
    pause
    exit /b 1
)

"%PYTHON%" -c "import asyncio, discord, aiosqlite, httpx, dotenv, database; asyncio.run(database.init_db())" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Required packages are missing or the environment is damaged.
    echo Reinstall requirements.lock and try again.
    pause
    exit /b 2
)

if /i "%~1"=="--check" (
    echo Environment check passed: %PYTHON%
    exit /b 0
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
