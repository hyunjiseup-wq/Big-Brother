"""Exercise real Windows shortcuts in a temporary APPDATA, never real Startup."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows shortcut integration")
class StartupCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bb startup ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name in ("register_startup.bat", "startup_shortcut.ps1", "시작프로그램_해제.bat"):
            shutil.copy2(ROOT / name, self.folder / name)
        self.launcher = self.folder / "run_bot_hidden.vbs"
        self.launcher.write_text('WScript.Quit 99\n', encoding="ascii")
        appdata = self.folder / "appdata"
        self.startup = appdata / "Microsoft/Windows/Start Menu/Programs/Startup"
        self.startup.mkdir(parents=True)
        self.shortcut = self.startup / "Discord AutoMod Bot.lnk"
        self.env = {**os.environ, "APPDATA": str(appdata), "AUTOMOD_HEADLESS": "1",
                    "TEST_SHORTCUT": str(self.shortcut)}

    def batch(self, *args, filename="register_startup.bat"):
        cmd = os.environ.get("COMSPEC", "cmd.exe")
        command = f'call "{self.folder / filename}" ' + " ".join(args)
        return subprocess.run(
            f'"{cmd}" /d /s /c "{command}"', cwd=self.folder, env=self.env,
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def assert_code(self, expected, *args, **kwargs):
        result = self.batch(*args, **kwargs)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def change_shortcut(self, expression):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "$ErrorActionPreference='Stop'; $ws=New-Object -ComObject WScript.Shell; "
             "$s=$ws.CreateShortcut($env:TEST_SHORTCUT); " + expression + "; $s.Save()"],
            env=self.env, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_register_check_remove_and_idempotent_remove(self):
        self.assert_code(2, "--check")
        self.assert_code(0)
        self.assert_code(0, "--check")
        self.assert_code(0, filename="시작프로그램_해제.bat")
        self.assertFalse(self.shortcut.exists())
        self.assert_code(0, "--remove")
        self.assert_code(2, "--check")

    def test_unknown_and_extra_options_never_register(self):
        for args in (("--typo",), ("--check", "extra"), ("--remove", "extra")):
            with self.subTest(args=args):
                self.assert_code(6, *args)
                self.assertFalse(self.shortcut.exists())

    def test_foreign_shortcut_is_not_changed_or_deleted(self):
        mutations = (
            ("$s.TargetPath=Join-Path $env:SystemRoot 'System32\\cmd.exe'", 3),
            ("$s.WorkingDirectory=$env:SystemRoot", 4),
            ("$s.Arguments='//B //NoLogo C:\\other\\run_bot_hidden.vbs'", 5),
        )
        for expression, expected in mutations:
            with self.subTest(expression=expression):
                if self.shortcut.exists():
                    self.shortcut.unlink()
                self.assert_code(0)
                self.change_shortcut(expression)
                before = self.shortcut.read_bytes()
                for args in ((), ("--check",), ("--remove",)):
                    self.assert_code(expected, *args)
                    self.assertEqual(self.shortcut.read_bytes(), before)

    def test_missing_launcher_still_allows_check_and_removal(self):
        self.assert_code(0)
        self.launcher.unlink()
        self.assert_code(0, "--check")
        self.assert_code(0, "--remove")
        self.assert_code(1)
        self.assertFalse(self.shortcut.exists())

    def test_invalid_window_style_can_be_repaired_or_removed(self):
        self.assert_code(0)
        self.change_shortcut("$s.WindowStyle=1")
        self.assert_code(6, "--check")
        self.assert_code(0)
        self.assert_code(0, "--check")
        self.change_shortcut("$s.WindowStyle=1")
        self.assert_code(0, "--remove")
        self.assertFalse(self.shortcut.exists())

    def test_shortcut_write_failure_is_not_success(self):
        self.shortcut.mkdir()
        self.assertNotEqual(self.batch().returncode, 0)
        self.assertTrue(self.shortcut.is_dir())


if __name__ == "__main__":
    unittest.main()
