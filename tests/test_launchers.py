"""Windows 실행/자동 시작 배치 파일의 핵심 안전 설정 회귀 테스트."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartupLauncherTests(unittest.TestCase):
    def test_startup_check_validates_target_workdir_and_arguments(self):
        script = (ROOT / "register_startup.bat").read_text(encoding="utf-8")
        self.assertIn("$actualTarget -ne $expectedTarget", script)
        self.assertIn("$actualWork -ne $expectedWork", script)
        self.assertIn("IsNullOrWhiteSpace($s.Arguments)", script)

    def test_registration_sets_target_and_working_directory(self):
        script = (ROOT / "register_startup.bat").read_text(encoding="utf-8")
        self.assertIn("$s.TargetPath=$env:AUTOMOD_LAUNCHER", script)
        self.assertIn("$s.WorkingDirectory=$env:AUTOMOD_WORKDIR", script)


class BotLauncherTests(unittest.TestCase):
    def test_environment_check_includes_database_integrity(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn("database.validate_database_integrity()", script)

    def test_manual_database_backup_option_is_available(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn('if /i "%~1"=="--backup-db"', script)
        self.assertIn("database.create_database_backup()", script)

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
