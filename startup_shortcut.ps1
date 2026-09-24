param([ValidateSet('Register', 'Check', 'Remove')][string]$Action = 'Check')

$ErrorActionPreference = 'Stop'
try {
    $shortcutPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\Discord AutoMod Bot.lnk'
    $expectedTarget = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $expectedWork = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
    $scriptPath = Join-Path $PSScriptRoot 'run_bot_hidden.vbs'
    $expectedArguments = '//B //NoLogo "' + $scriptPath + '"'
    $exists = Test-Path -LiteralPath $shortcutPath

    if (-not $exists -and $Action -ne 'Register') {
        Write-Output '[NOT REGISTERED] Startup shortcut was not found.'
        if ($Action -eq 'Check') { exit 2 }
        exit 0
    }

    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($shortcutPath)
    if ($exists) {
        # Never overwrite or delete a shortcut owned by another installation.
        $actualTarget = if ($s.TargetPath) { [IO.Path]::GetFullPath($s.TargetPath) } else { '' }
        $actualWork = if ($s.WorkingDirectory) { [IO.Path]::GetFullPath($s.WorkingDirectory).TrimEnd('\') } else { '' }
        if ($actualTarget -ne $expectedTarget) { Write-Output '[INVALID] Different startup host; no changes made.'; exit 3 }
        if ($actualWork -ne $expectedWork) { Write-Output '[INVALID] Different working directory; no changes made.'; exit 4 }
        if ($s.Arguments -ne $expectedArguments) { Write-Output '[INVALID] Unexpected arguments; no changes made.'; exit 5 }
    }

    if ($Action -eq 'Check') {
        if ($s.WindowStyle -ne 7) { Write-Output '[INVALID] Startup window style is not minimized.'; exit 6 }
        Write-Output '[OK] Hidden bot startup is registered.'
    } elseif ($Action -eq 'Remove') {
        Remove-Item -LiteralPath $shortcutPath -ErrorAction Stop
        Write-Output '[OK] Startup registration removed. Running bot was not stopped.'
    } else {
        if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw 'run_bot_hidden.vbs was not found.' }
        $s.TargetPath = $expectedTarget
        $s.Arguments = $expectedArguments
        $s.WorkingDirectory = $expectedWork
        $s.WindowStyle = 7
        $s.Save()
        Write-Output '[OK] Hidden bot startup registered.'
    }
    exit 0
} catch {
    Write-Output ('[ERROR] Startup operation failed: ' + $_.Exception.Message)
    exit 1
}
