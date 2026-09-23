@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if /i "%~1"=="--check" goto check

if not "%~1"=="" (
    echo [ERROR] Unknown option: %~1
    echo Supported option: --check
    exit /b 2
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_bot.ps1"
set "STOP_EXIT=%errorlevel%"
if not "%STOP_EXIT%"=="0" pause
exit /b %STOP_EXIT%

:check
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_bot.ps1" -Check
exit /b %errorlevel%
