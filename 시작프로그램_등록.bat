@echo off
call "%~dp0register_startup.bat" %*
exit /b %errorlevel%
