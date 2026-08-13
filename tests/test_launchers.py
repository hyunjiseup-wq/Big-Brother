"""Windows 실행/자동 시작 배치 파일의 핵심 안전 설정 회귀 테스트."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartupLauncherTests(unittest.TestCase):
    def test_startup_check_validates_target_workdir_and_arguments(self):
        script = (ROOT / "register_startup.bat").read_text(encoding="utf-8")
        self.assertIn("$actualTarget -ne $expectedTarget", script)
        self.assertIn("$actualWork -ne $expectedWork", script)
        self.assertIn("$s.Arguments -ne $env:AUTOMOD_ARGUMENTS", script)
        self.assertIn("$s.WindowStyle -ne 7", script)

    def test_registration_sets_target_and_working_directory(self):
        script = (ROOT / "register_startup.bat").read_text(encoding="utf-8")
        self.assertIn("$s.TargetPath=$env:AUTOMOD_HOST", script)
        self.assertIn("$s.Arguments=$env:AUTOMOD_ARGUMENTS", script)
        self.assertIn("$s.WorkingDirectory=$env:AUTOMOD_WORKDIR", script)
        self.assertIn("$s.WindowStyle=7", script)

    def test_hidden_launcher_uses_no_window_and_rotating_log(self):
        script = (ROOT / "run_bot_hidden.vbs").read_text(encoding="utf-8")
        self.assertIn('processEnv("AUTOMOD_HEADLESS") = "1"', script)
        self.assertIn("shell.Run command, 0, False", script)
        self.assertIn("bot_runtime.log", script)
        self.assertIn("5242880", script)


class BotLauncherTests(unittest.TestCase):
    def test_headless_mode_never_waits_for_keyboard_input(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        pause_lines = [line.strip() for line in script.splitlines() if "pause" in line]
        self.assertTrue(pause_lines)
        self.assertTrue(all(line == "if not defined AUTOMOD_HEADLESS pause" for line in pause_lines))

    def test_environment_check_includes_database_integrity(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn("database.validate_database_integrity()", script)

    def test_manual_database_backup_option_is_available(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn('if /i "%~1"=="--backup-db"', script)
        self.assertIn("database.create_database_backup()", script)
        self.assertLess(
            script.index('if /i "%~1"=="--backup-db"'),
            script.index("database.init_db()"),
        )

    def test_unknown_option_is_rejected_before_bot_loop(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn('if not "%~1"==""', script)
        self.assertIn("exit /b 6", script)
        self.assertLess(script.index("Unknown option"), script.index("set /a RETRIES=0"))

    def test_local_runtime_status_option_is_available(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn('if /i "%~1"=="--status"', script)
        self.assertIn("runtime_status.py", script)

    def test_normal_exit_does_not_restart(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn('if "%BOT_EXIT%"=="0" goto stopped', script)
        self.assertIn(":stopped", script)
        self.assertIn("exit /b 0", script)

    def test_stable_runtime_resets_consecutive_failure_count(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn("BOT_RUNTIME=BOT_STOPPED_AT-BOT_STARTED_AT", script)
        self.assertIn("if %BOT_RUNTIME% GEQ 300 set /a RETRIES=0", script)


if __name__ == "__main__":
    unittest.main()
