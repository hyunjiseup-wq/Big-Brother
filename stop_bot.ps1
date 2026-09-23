param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd("\")
$pythonPath = [IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "DiscordAutoMod\venv-3.13\Scripts\python.exe")
)
$stopFile = Join-Path $root ".automod-stop-request"

function Test-AutomodRunner($process, [switch]$AllowInteractive) {
    if ($process.Name -ne 'cmd.exe' -or -not $process.CommandLine) { return $false }
    # Match the executed command, not a path mentioned in echo, another script,
    # a diagnostic invocation, or a similarly named checkout. /k may own a bot,
    # but its interactive shell must never be forcibly closed.
    $mode = if ($AllowInteractive) { '[ck]' } else { 'c' }
    $launcherNames = @('run_bot.bat', ((-join [char[]](0xBD07, 0xC2E4, 0xD589)) + '.bat'))
    $launchers = ($launcherNames | ForEach-Object { [regex]::Escape((Join-Path $root $_)) }) -join '|'
    $pattern = '(?i)^\s*(?:"[^"]*cmd\.exe"|\S*cmd(?:\.exe)?)\s+(?:/[dsq]\s+)*/' + $mode
    $pattern += '\s+"?(?:call\s+)?"?(?:' + $launchers + ')"?\s*(?:>>?\s*(?:"[^"\r\n]+"|[^\s"&|<>]+)(?:\s+2>&1)?)?"?\s*$'
    return $process.CommandLine -match $pattern
}

function Get-AutomodProcesses {
    $all = @(Get-CimInstance Win32_Process)
    $selfProcess = $all | Where-Object { [int]$_.ProcessId -eq $PID } | Select-Object -First 1
    $selfParentId = if ($selfProcess) { [int]$selfProcess.ParentProcessId } else { -1 }
    $runners = @($all | Where-Object {
        [int]$_.ProcessId -ne $selfParentId -and
        (Test-AutomodRunner $_)
    })
    $bots = @()
    $ambiguous = @()
    foreach ($process in $all) {
        if ($process.Name -notin @('python.exe', 'pythonw.exe') -or -not $process.ExecutablePath) { continue }
        if (-not [IO.Path]::GetFullPath($process.ExecutablePath).Equals($pythonPath, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($process.CommandLine -notmatch '^\s*(?:"[^"]+"|\S+)\s+(?:-(?:u|B)\s+)*(?:"(?<script>[^"]+)"|(?<script>[^\s"]+))\s*$') { continue }
        $scriptPath = $Matches.script
        if ([IO.Path]::IsPathRooted($scriptPath)) {
            if ([IO.Path]::GetFullPath($scriptPath).Equals((Join-Path $root 'bot.py'), [StringComparison]::OrdinalIgnoreCase)) {
                $bots += $process
            }
        } elseif ($scriptPath -in @('bot.py', '.\bot.py')) {
            $parent = $all | Where-Object { $_.ProcessId -eq $process.ParentProcessId } | Select-Object -First 1
            if ($parent -and (Test-AutomodRunner $parent -AllowInteractive)) { $bots += $process }
            else { $ambiguous += $process }
        }
    }
    return @{ Bots = $bots; Runners = $runners; Ambiguous = $ambiguous }
}

function Stop-VerifiedProcess($expected) {
    # Recheck identity after the grace period. A reused PID is not our process.
    $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($expected.ProcessId)"
    if (-not $current) { return }
    if (-not $expected.CreationDate -or $current.CreationDate -ne $expected.CreationDate -or
        $current.ExecutablePath -ne $expected.ExecutablePath -or $current.CommandLine -ne $expected.CommandLine) {
        throw "Process identity changed for PID $($expected.ProcessId); refusing forced termination."
    }
    Stop-Process -Id $current.ProcessId -Force -ErrorAction Stop
}

try {
    $targets = Get-AutomodProcesses
} catch {
    Write-Host "[ERROR] Unable to inspect the Big Brother process: $($_.Exception.Message)"
    exit 2
}

if ($Check) {
    if ($targets.Ambiguous.Count -gt 0) {
        Write-Host '[ERROR] A relative bot.py process has no verifiable project path. Status is unknown.'
        exit 2
    }
    if ($targets.Bots.Count -eq 0 -and $targets.Runners.Count -eq 0) {
        Write-Host "[STOPPED] Big Brother is not running."
        exit 1
    }
    foreach ($process in $targets.Bots) {
        Write-Host "[RUNNING] Bot process PID $($process.ProcessId)"
    }
    foreach ($process in $targets.Runners) {
        Write-Host "[RUNNING] Restart runner PID $($process.ProcessId)"
    }
    exit 0
}

if ($targets.Ambiguous.Count -gt 0) {
    Write-Host '[ERROR] Cannot verify the project of a relative bot.py process. No process was stopped.'
    exit 2
}

if ($targets.Bots.Count -eq 0 -and $targets.Runners.Count -eq 0) {
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    Write-Host "[INFO] Big Brother is already stopped."
    exit 0
}

try {
[IO.File]::WriteAllText($stopFile, "stop", [Text.Encoding]::ASCII)
Write-Host "[INFO] A graceful stop was requested."

# Give the in-process watcher time to close Discord and shared HTTP clients.
$deadline = [DateTime]::UtcNow.AddSeconds(15)
do {
    Start-Sleep -Milliseconds 250
    $alive = @($targets.Bots | Where-Object {
        Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
    })
} while ($alive.Count -gt 0 -and [DateTime]::UtcNow -lt $deadline)

if ($alive.Count -gt 0) {
    Write-Host "[WARN] Graceful shutdown timed out. Stopping only the matching bot process."
    foreach ($process in $alive) {
        Stop-VerifiedProcess $process
    }
}

# Normally run_bot.bat consumes the marker and exits by itself. An already-running
# legacy copy may not know about the marker, so stop only the precisely identified
# runner if it remains after a short grace period. This also closes a retry gap.
$runnerDeadline = [DateTime]::UtcNow.AddSeconds(3)
do {
    Start-Sleep -Milliseconds 250
    $aliveRunners = @($targets.Runners | Where-Object {
        Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
    })
} while ($aliveRunners.Count -gt 0 -and [DateTime]::UtcNow -lt $runnerDeadline)

foreach ($process in $aliveRunners) {
    Stop-VerifiedProcess $process
}

$remaining = Get-AutomodProcesses
if ($remaining.Bots.Count -gt 0 -or $remaining.Runners.Count -gt 0 -or $remaining.Ambiguous.Count -gt 0) {
    throw 'Bot or restart runner is still present; retry stop after checking permissions.'
}
if (Test-Path -LiteralPath $stopFile) { Remove-Item -LiteralPath $stopFile -Force -ErrorAction Stop }
Write-Host "[OK] Big Brother stop processing completed."
exit 0
} catch {
    Write-Host "[ERROR] Stop was not confirmed: $($_.Exception.Message)"
    try {
        [IO.File]::WriteAllText($stopFile, "stop", [Text.Encoding]::ASCII)
        Write-Host '[INFO] The stop request is retained. Retry after resolving the error.'
    } catch {
        Write-Host '[ERROR] Unable to retain the stop request; check folder permissions.'
    }
    exit 2
}
