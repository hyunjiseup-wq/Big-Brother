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


if __name__ == "__main__":
    unittest.main()
