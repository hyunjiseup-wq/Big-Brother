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

"%PYTHON%" -c "import asyncio, database; asyncio.run(database.init_db()); asyncio.run(database.validate_database_integrity())"
if errorlevel 1 (
    echo [ERROR] The database could not be initialized or failed its integrity check.
    echo Check AUTOMOD_DB_PATH, folder permissions, and database backups. The detailed error is shown above.
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

if /i "%~1"=="--backup-db" (
    "%PYTHON%" -c "import asyncio, database; p=asyncio.run(database.create_database_backup()); print('Database backup created: '+str(p))"
    exit /b %errorlevel%
)

if /i "%~1"=="--check-network" (
    "%PYTHON%" network_check.py
    exit /b %errorlevel%
)

set /a RETRIES=0
:loop
echo [%date% %time%] Starting bot...
for /f %%T in ('powershell -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()"') do set "BOT_STARTED_AT=%%T"
"%PYTHON%" bot.py
set "BOT_EXIT=%errorlevel%"
if "%BOT_EXIT%"=="0" goto stopped
if "%BOT_EXIT%"=="3" goto duplicate

for /f %%T in ('powershell -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()"') do set "BOT_STOPPED_AT=%%T"
set /a BOT_RUNTIME=BOT_STOPPED_AT-BOT_STARTED_AT
if %BOT_RUNTIME% GEQ 300 set /a RETRIES=0
set /a RETRIES+=1
if %RETRIES% GEQ 5 goto failed
echo [%date% %time%] Bot exited with code %BOT_EXIT%. Restarting in 10 seconds. (%RETRIES%/5 consecutive failures)
timeout /t 10 /nobreak >nul
goto loop

:stopped
echo [INFO] The bot stopped normally. Automatic restart is not required.
exit /b 0

:duplicate
echo [INFO] The bot is already running.
pause
exit /b 3

:failed
echo [ERROR] Bot stopped five times. Automatic restart has been disabled.
echo Review the console error and configuration.
pause
exit /b %BOT_EXIT%
