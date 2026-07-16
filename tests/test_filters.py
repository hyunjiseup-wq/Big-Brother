import unittest

import config
import filters


class FilterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
