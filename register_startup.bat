@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0run_bot_hidden.vbs" (
    echo [ERROR] run_bot_hidden.vbs was not found.
    pause
    exit /b 1
)

set "AUTOMOD_HOST=%SystemRoot%\System32\wscript.exe"
set "AUTOMOD_SCRIPT=%~dp0run_bot_hidden.vbs"
set AUTOMOD_ARGUMENTS=//B //NoLogo "%AUTOMOD_SCRIPT%"
set "AUTOMOD_WORKDIR=%~dp0"
set "AUTOMOD_SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Discord AutoMod Bot.lnk"

if /i "%~1"=="--check" (
    powershell -NoProfile -Command "$p=$env:AUTOMOD_SHORTCUT; if(-not (Test-Path -LiteralPath $p)){Write-Output '[NOT REGISTERED] Startup shortcut was not found.'; exit 2}; $ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut($p); $expectedTarget=[IO.Path]::GetFullPath($env:AUTOMOD_HOST); $expectedWork=[IO.Path]::GetFullPath($env:AUTOMOD_WORKDIR).TrimEnd('\'); $actualTarget=[IO.Path]::GetFullPath($s.TargetPath); $actualWork=if($s.WorkingDirectory){[IO.Path]::GetFullPath($s.WorkingDirectory).TrimEnd('\')}else{''}; if($actualTarget -ne $expectedTarget){Write-Output '[INVALID] Startup shortcut points to a different host.'; exit 3}; if($actualWork -ne $expectedWork){Write-Output '[INVALID] Startup shortcut has a different working directory.'; exit 4}; if($s.Arguments -ne $env:AUTOMOD_ARGUMENTS){Write-Output '[INVALID] Startup shortcut has unexpected arguments.'; exit 5}; if($s.WindowStyle -ne 7){Write-Output '[INVALID] Startup shortcut is not configured as hidden/minimized.'; exit 6}; Write-Output ('[OK] Hidden startup host: '+$s.TargetPath); Write-Output ('[OK] Script: '+$env:AUTOMOD_SCRIPT); Write-Output ('[OK] Working directory: '+$s.WorkingDirectory); exit 0"
    goto check_exit
)

powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut($env:AUTOMOD_SHORTCUT); $s.TargetPath=$env:AUTOMOD_HOST; $s.Arguments=$env:AUTOMOD_ARGUMENTS; $s.WorkingDirectory=$env:AUTOMOD_WORKDIR; $s.WindowStyle=7; $s.Save()"
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
