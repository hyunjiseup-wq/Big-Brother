@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not "%~2"=="" goto invalid_option
if "%~1"=="" goto register
if /i "%~1"=="--check" goto check
if /i "%~1"=="--remove" goto remove
goto invalid_option

:register
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0startup_shortcut.ps1" -Action Register
set "RESULT=%errorlevel%"
if not defined AUTOMOD_HEADLESS pause
exit /b %RESULT%

:check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0startup_shortcut.ps1" -Action Check
exit /b %errorlevel%

:remove
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0startup_shortcut.ps1" -Action Remove
exit /b %errorlevel%

:invalid_option
echo [ERROR] Unknown option. Usage: register_startup.bat [--check ^| --remove]
exit /b 6
