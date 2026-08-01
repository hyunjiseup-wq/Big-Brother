@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0run_bot.bat" (
    echo [ERROR] run_bot.bat was not found.
    pause
    exit /b 1
)

set "AUTOMOD_LAUNCHER=%~dp0run_bot.bat"
set "AUTOMOD_WORKDIR=%~dp0"
set "AUTOMOD_SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Discord AutoMod Bot.lnk"

if /i "%~1"=="--check" (
    powershell -NoProfile -Command "$p=$env:AUTOMOD_SHORTCUT; if(-not (Test-Path -LiteralPath $p)){Write-Output '[NOT REGISTERED] Startup shortcut was not found.'; exit 2}; $ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut($p); $expectedTarget=[IO.Path]::GetFullPath($env:AUTOMOD_LAUNCHER); $expectedWork=[IO.Path]::GetFullPath($env:AUTOMOD_WORKDIR).TrimEnd('\'); $actualTarget=[IO.Path]::GetFullPath($s.TargetPath); $actualWork=if($s.WorkingDirectory){[IO.Path]::GetFullPath($s.WorkingDirectory).TrimEnd('\')}else{''}; if($actualTarget -ne $expectedTarget){Write-Output '[INVALID] Startup shortcut points to a different launcher.'; exit 3}; if($actualWork -ne $expectedWork){Write-Output '[INVALID] Startup shortcut has a different working directory.'; exit 4}; if(-not [string]::IsNullOrWhiteSpace($s.Arguments)){Write-Output '[INVALID] Startup shortcut has unexpected arguments.'; exit 5}; Write-Output ('[OK] Startup shortcut: '+$s.TargetPath); Write-Output ('[OK] Working directory: '+$s.WorkingDirectory); Write-Output '[OK] Arguments: none'; exit 0"
    goto check_exit
)

powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut($env:AUTOMOD_SHORTCUT); $s.TargetPath=$env:AUTOMOD_LAUNCHER; $s.WorkingDirectory=$env:AUTOMOD_WORKDIR; $s.Save()"
if errorlevel 1 (
    echo [ERROR] Failed to register Startup shortcut.
    pause
    exit /b 1
)
echo Startup registration completed.
pause
exit /b 0

:check_exit
exit /b %errorlevel%
