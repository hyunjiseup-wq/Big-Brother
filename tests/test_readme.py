from pathlib import Path
import unittest


README = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
README_TEXT = " ".join(README.split())


class ReadmeOperatingModeTests(unittest.TestCase):
    def test_readme_describes_current_manual_review_mode(self):
        required = (
            "# BB봇 — 디스코드 AI 관리자 검수 봇",
            "**수동 검수 모드**",
            "로컬 Ollama → Gemini → Groq",
            "AI나 정규식 필터가 자체적으로 경고·삭제·타임아웃·킥·밴을 실행하지",
            "관리자가 위반으로 확정하고 실제 조치가 성공한 경우에만",
            "킥과 밴도 AI가 실행하지 않습니다",
            "USER_SANCTION_DM_ENABLED=False",
            "MANUAL_REVIEW_USER_NOTICE_ENABLED=False",
        )
        for text in required:
            with self.subTest(text=text):
                self.assertIn(text, README_TEXT)

    def test_readme_does_not_restore_obsolete_automatic_sanction_claims(self):
        obsolete = (
            "AI(Gemini 1차 / Groq 2차 / 로컬 Ollama 3차)",
            "경고 → 삭제 → 타임아웃 → 킥 → 밴까지 단계적으로 자동 제재",
            "무료 한도가 모두 소진돼도 한도가 없는 로컬 모델이 마지막 폴백",
            "경고/삭제/타임아웃까지는 모델 종류와 무관하게 자동 실행",
            "실시간 자동제재",
        )
        for text in obsolete:
            with self.subTest(text=text):
                self.assertNotIn(text, README_TEXT)


if __name__ == "__main__":
    unittest.main()
