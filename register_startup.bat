@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0run_bot.bat" (
    echo [ERROR] run_bot.bat was not found.
    pause
    exit /b 1
)
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut([Environment]::GetFolderPath('Startup')+'\Discord AutoMod Bot.lnk'); $s.TargetPath='%~dp0run_bot.bat'; $s.WorkingDirectory='%~dp0'; $s.Save()"
if errorlevel 1 (
    echo [ERROR] Failed to register Startup shortcut.
    pause
    exit /b 1
)
echo Startup registration completed.
pause
