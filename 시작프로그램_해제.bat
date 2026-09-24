@echo off
call "%~dp0register_startup.bat" --remove %*
set "RESULT=%errorlevel%"
if not defined AUTOMOD_HEADLESS pause
exit /b %RESULT%
