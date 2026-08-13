import unittest

import config
import filters


class FilterTests(unittest.TestCase):
    def test_promo_code_text_is_not_fast_filtered_as_advertising(self):
        result = filters.fast_check(101, 202, "타르코프 런처 이벤트 코드 ROADTORELEASE 쓰세요")
        self.assertEqual(result.decision, "NEEDS_AI")

    def setUp(self):
        filters._recent_messages.clear()
        filters._call_counter = 0

    def test_short_korean_banned_words_are_not_skipped(self):
        for index, text in enumerate(("씨발", "ㅅㅂ", "창녀"), start=1):
            with self.subTest(text=text):
                result = filters.fast_check(1, index, text)
                self.assertEqual(result.decision, "DECIDED")
                self.assertNotEqual(result.level, "NONE")

    def test_short_spam_is_detected(self):
        result = None
        for _ in range(config.SPAM_REPEAT_THRESHOLD):
            result = filters.fast_check(1, 10, "ㅋ")
        self.assertEqual(result.decision, "DECIDED")
        self.assertEqual(result.level, "MODERATE")

    def test_zero_width_character_does_not_bypass_filter(self):
        result = filters.fast_check(1, 20, "씨\u200b발")
        self.assertEqual(result.decision, "DECIDED")

    def test_verified_internal_invite_can_bypass_only_the_invite_rule(self):
        url = "https://discord.gg/abc123"
        blocked = filters.fast_check(1, 30, f"같이 하실 분 {url}")
        allowed = filters.fast_check(1, 31, f"같이 하실 분 {url}", allow_discord_invites=True)
        self.assertEqual(blocked.decision, "DECIDED")
        self.assertEqual(allowed.decision, "NEEDS_AI")

    def test_extracts_invite_without_trailing_chat_punctuation(self):
        self.assertEqual(
            filters.extract_discord_invite_urls("여기로 오세요 (https://discord.gg/abc123)."),
            ["https://discord.gg/abc123"],
        )


if __name__ == "__main__":
    unittest.main()
