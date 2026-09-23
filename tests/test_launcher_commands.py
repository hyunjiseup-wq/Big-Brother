"""Execute Windows launchers against isolated stubs; never start the real bot."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows BAT command integration")
class LauncherCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bb launcher ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["AUTOMOD_HEADLESS"] = "1"
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def run_batch(self, filename, *args):
        # cmd.exe does not use the backslash-quote escaping of list2cmdline.
        command = f'call "{self.folder / filename}" ' + " ".join(args)
        cmd = os.environ.get("COMSPEC", "cmd.exe")
        return subprocess.run(
            f'"{cmd}" /d /s /c "{command}"',
            cwd=self.folder, env=self.env, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def prepare_run_launcher(self):
        source = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        # Use this test interpreter, but all imported modules live in the temp dir.
        source = source.replace(
            'set "PYTHON=%LOCALAPPDATA%\\DiscordAutoMod\\venv-3.13\\Scripts\\python.exe"',
            f'set "PYTHON={sys.executable}"',
        )
        (self.folder / "run_bot.bat").write_text(source, encoding="utf-8")
        for module in ("discord", "aiosqlite", "httpx", "dotenv"):
            (self.folder / f"{module}.py").write_text("", encoding="utf-8")
        (self.folder / "database.py").write_text(
            "async def init_db(): pass\n"
            "async def validate_database_integrity(): pass\n"
            "async def create_database_backup(): return 'fixture.db'\n",
            encoding="utf-8",
        )
        (self.folder / "bot.py").write_text(
            "def validate_runtime_environment(): pass\n"
            "if __name__ == '__main__': raise RuntimeError('Bot must not start')\n",
            encoding="utf-8",
        )
        (self.folder / "runtime_status.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        (self.folder / "network_check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    def test_stop_check_returns_child_status_through_both_entrypoints(self):
        shutil.copy2(ROOT / "stop_bot.bat", self.folder / "stop_bot.bat")
        shutil.copy2(ROOT / "작동중지.bat", self.folder / "작동중지.bat")
        for code in (0, 1, 2):
            (self.folder / "stop_bot.ps1").write_text(
                f"param([switch]$Check)\nexit {code}\n", encoding="ascii",
            )
            for entrypoint in ("stop_bot.bat", "작동중지.bat"):
                with self.subTest(code=code, entrypoint=entrypoint):
                    result = self.run_batch(entrypoint, "--check")
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def test_diagnostics_preserve_pending_stop_request(self):
        self.prepare_run_launcher()
        marker = self.folder / ".automod-stop-request"
        for option, expected in (("--status", 1), ("--check", 0),
                                 ("--backup-db", 0), ("--check-network", 0), ("--bad-option", 6)):
            with self.subTest(option=option):
                marker.write_text("pending shutdown", encoding="ascii")
                result = self.run_batch("run_bot.bat", option)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertTrue(marker.exists(), f"{option} erased the stop request")
                self.assertEqual(marker.read_text(encoding="ascii"), "pending shutdown")

    def test_invalid_option_does_not_initialize_database(self):
        self.prepare_run_launcher()
        (self.folder / "database.py").write_text(
            "from pathlib import Path\n"
            "Path('database-was-imported').touch()\n"
            "raise RuntimeError('Invalid options must not access the DB')\n", encoding="utf-8",
        )
        result = self.run_batch("run_bot.bat", "--bad-option")
        self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
        self.assertFalse((self.folder / "database-was-imported").exists())

    def test_explicit_start_clears_stale_marker_and_runs_once(self):
        self.prepare_run_launcher()
        marker = self.folder / ".automod-stop-request"
        marker.write_text("stale", encoding="ascii")
        (self.folder / "bot.py").write_text(
            "from pathlib import Path\n"
            "def validate_runtime_environment(): pass\n"
            "if __name__ == '__main__':\n"
            "    assert not Path('.automod-stop-request').exists()\n"
            "    with Path('starts.txt').open('a') as f: f.write('start\\n')\n",
            encoding="utf-8",
        )
        result = self.run_batch("run_bot.bat")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual((self.folder / "starts.txt").read_text(), "start\n")
