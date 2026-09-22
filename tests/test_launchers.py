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

    def test_command_modes_propagate_the_real_child_exit_code(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn("setlocal EnableExtensions EnableDelayedExpansion", script)
        self.assertEqual(script.count("exit /b !errorlevel!"), 3)

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

    def test_stop_request_prevents_automatic_restart(self):
        script = (ROOT / "run_bot.bat").read_text(encoding="utf-8")
        self.assertIn('set "AUTOMOD_STOP_FILE=%~dp0.automod-stop-request"', script)
        self.assertIn('if exist "%AUTOMOD_STOP_FILE%" goto stop_requested', script)
        self.assertLess(
            script.index('if exist "%AUTOMOD_STOP_FILE%" goto stop_requested'),
            script.index('if "%BOT_EXIT%"=="0" goto stopped'),
        )

    def test_stop_launcher_targets_only_the_dedicated_bot_python(self):
        script = (ROOT / "stop_bot.ps1").read_text(encoding="utf-8")
        self.assertIn('DiscordAutoMod\\venv-3.13\\Scripts\\python.exe', script)
        self.assertIn("$_.CommandLine", script)
        self.assertIn("$_.ExecutablePath", script)
        self.assertNotIn("taskkill /im python.exe", script.casefold())
        self.assertLess(script.index("AddSeconds(15)"), script.index("Stop-Process"))

    def test_korean_stop_shortcut_calls_the_checked_launcher(self):
        script = (ROOT / "작동중지.bat").read_text(encoding="utf-8")
        self.assertIn('call "%~dp0stop_bot.bat" %*', script)


if __name__ == "__main__":
    unittest.main()
