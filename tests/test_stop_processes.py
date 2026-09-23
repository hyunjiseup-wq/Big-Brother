"""Run the stop script with synthetic process APIs; never inspect/kill real processes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows stop controller")
class StopProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bb stop ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.python = str(Path(os.environ["LOCALAPPDATA"]) / "DiscordAutoMod/venv-3.13/Scripts/python.exe")
        self.cmd = r"C:\Windows\System32\cmd.exe"

    def process(self, pid, command, *, name="python.exe", executable=None, parent=0):
        return dict(ProcessId=pid, ParentProcessId=parent, Name=name,
                    ExecutablePath=executable or self.python, CommandLine=command,
                    CreationDate="2026-09-24T00:00:00Z")

    def runner(self, pid=20, *, mode="c", filename="run_bot.bat", suffix=""):
        return self.process(pid, f'"{self.cmd}" /d /{mode} call "{self.folder / filename}"{suffix}',
                            name="cmd.exe", executable=self.cmd)

    def run_controller(self, processes, *, check=True, behavior="normal"):
        source = (ROOT / "stop_bot.ps1").read_text(encoding="utf-8")
        # Exercise force/confirmation paths immediately with fake APIs, not a 15s sleep.
        source = source.replace("AddSeconds(15)", "AddSeconds(0)").replace("AddSeconds(3)", "AddSeconds(0)")
        (self.folder / "stop_bot.ps1").write_text(source, encoding="utf-8-sig")
        (self.folder / "processes.json").write_text(json.dumps(processes), encoding="utf-8")
        harness = r'''
$ErrorActionPreference = 'Stop'
$script:items = Get-Content -Raw (Join-Path $PSScriptRoot 'processes.json') | ConvertFrom-Json
$script:behavior = '__BEHAVIOR__'
$script:terminated = $false
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($script:behavior -eq 'query-error') { throw 'query denied' }
    if ($Filter) {
        $number = [int]($Filter.Split('=')[1])
        $item = $script:items | Where-Object { $_.ProcessId -eq $number }
        if ($script:behavior -eq 'reused' -and $item) {
            $copy = $item | ConvertTo-Json | ConvertFrom-Json
            $copy.CreationDate = '2026-09-24T00:01:00Z'
            return $copy
        }
        return $item
    }
    return $script:items
}
function Get-Process { param($Id, $ErrorAction) return $script:items | Where-Object { $_.ProcessId -eq $Id } }
function Start-Sleep {
    param($Milliseconds)
    if ($script:behavior -eq 'graceful') {
        $script:items = @()
        Remove-Item -LiteralPath (Join-Path $PSScriptRoot '.automod-stop-request') -ErrorAction SilentlyContinue
    }
}
function Stop-Process {
    param($Id, [switch]$Force, $ErrorAction)
    Add-Content -LiteralPath (Join-Path $PSScriptRoot 'stop-calls.txt') -Value $Id
    if ($script:behavior -eq 'denied') { throw 'access denied' }
    if ($script:behavior -eq 'survives') { return }
    $script:items = @($script:items | Where-Object { $_.ProcessId -ne $Id })
}
. (Join-Path $PSScriptRoot 'stop_bot.ps1') __CHECK__
exit $LASTEXITCODE
'''.replace("__BEHAVIOR__", behavior).replace("__CHECK__", "-Check" if check else "")
        (self.folder / "harness.ps1").write_text(harness, encoding="utf-8-sig")
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.folder / "harness.ps1")],
            capture_output=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def test_check_identifies_only_exact_project_and_launchers(self):
        own = self.process(10, f'"{self.python}" "{self.folder / "bot.py"}"')
        fixtures = [own, self.runner(), self.runner(21, filename="봇실행.bat"),
                    self.runner(22, suffix=f' >> "{self.folder / "logs/runtime.log"}" 2>&1')]
        fixtures += [self.runner(30, filename="stop_bot.bat"), self.runner(31, filename="other.bat"),
                     self.runner(32, suffix=" --status"), self.runner(33, mode="k"),
                     self.runner(34, suffix=" & echo unrelated")]
        fixtures += [self.process(40, f'"{self.python}" "{self.folder}-other\\bot.py"'),
                     self.process(41, f'"{self.python}" "{self.folder / "other-bot.py"}"'),
                     self.process(42, f'"{self.python}" -c "print(\'bot.py\')"'),
                     self.process(43, f'"other-python.exe" "{self.folder / "bot.py"}"', executable=r'C:\other\python.exe')]
        result = self.run_controller(fixtures)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for pid in (10, 20, 21, 22):
            self.assertIn(f"PID {pid}".encode(), result.stdout)
        for pid in (30, 31, 32, 33, 34, 40, 41, 42, 43):
            self.assertNotIn(f"PID {pid}".encode(), result.stdout)
        self.assertFalse((self.folder / ".automod-stop-request").exists())
        self.assertFalse((self.folder / "stop-calls.txt").exists())

    def test_legacy_relative_bot_requires_known_parent_and_keeps_interactive_shell(self):
        fixtures = [self.runner(mode="k"), self.process(10, f'"{self.python}" bot.py', parent=20)]
        result = self.run_controller(fixtures, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.folder / "stop-calls.txt").read_text().strip(), "10")

    def test_ambiguous_relative_bot_is_not_stopped(self):
        result = self.run_controller([self.process(10, f'"{self.python}" bot.py')], check=False)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.folder / "stop-calls.txt").exists())

    def test_graceful_exit_accepts_marker_already_consumed_by_runner(self):
        result = self.run_controller([self.runner()], check=False, behavior="graceful")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.folder / "stop-calls.txt").exists())

    def test_failed_stop_retains_marker_and_returns_failure(self):
        for behavior in ("denied", "survives", "reused"):
            with self.subTest(behavior=behavior):
                calls = self.folder / "stop-calls.txt"
                calls.unlink(missing_ok=True)
                result = self.run_controller([self.runner()], check=False, behavior=behavior)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn(b"[OK]", result.stdout)
                self.assertTrue((self.folder / ".automod-stop-request").exists())
                if behavior == "reused":
                    self.assertFalse(calls.exists())

    def test_query_failure_and_stopped_state_are_distinct(self):
        for behavior, expected in (("normal", 1), ("query-error", 2)):
            with self.subTest(behavior=behavior):
                result = self.run_controller([], behavior=behavior)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
