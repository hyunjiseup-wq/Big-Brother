param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd("\")
$pythonPath = [IO.Path]::GetFullPath(
    (Join-Path $env:LOCALAPPDATA "DiscordAutoMod\venv-3.13\Scripts\python.exe")
)
$stopFile = Join-Path $root ".automod-stop-request"

function Get-AutomodProcesses {
    $all = @(Get-CimInstance Win32_Process)
    $bots = @($all | Where-Object {
        $_.Name -in @("python.exe", "pythonw.exe") -and
        $_.ExecutablePath -and
        [IO.Path]::GetFullPath($_.ExecutablePath).Equals(
            $pythonPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        $_.CommandLine -match "(?i)\bbot\.py\b"
    })

    # The runner may be between retries with no Python child for up to ten seconds.
    # Exclude this stop script's parent cmd.exe and identify only batch processes
    # launched from the bot directory.
    $selfProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
    $selfParentId = [int]$selfProcess.ParentProcessId
    $runners = @($all | Where-Object {
        $_.Name -eq "cmd.exe" -and
        [int]$_.ProcessId -ne $selfParentId -and
        $_.CommandLine -and
        $_.CommandLine.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.CommandLine -match '(?i)\.bat(?:"|\s|$)'
    })
    return @{ Bots = $bots; Runners = $runners }
}

try {
    $targets = Get-AutomodProcesses
} catch {
    Write-Host "[ERROR] Unable to inspect the Big Brother process: $($_.Exception.Message)"
    exit 2
}

if ($Check) {
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

if ($targets.Bots.Count -eq 0 -and $targets.Runners.Count -eq 0) {
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    Write-Host "[INFO] Big Brother is already stopped."
    exit 0
}

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
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
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
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
Write-Host "[OK] Big Brother stop processing completed."
exit 0
