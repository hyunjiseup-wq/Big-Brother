import unittest

from language_detection import detect_language_group


class LanguageDetectionTests(unittest.TestCase):
    def test_common_language_groups(self):
        cases = {
            "안녕하세요 반갑습니다": "ko",
            "hello everyone": "latin",
            "こんにちは世界": "ja",
            "你好世界": "zh",
            "привет мир": "cyrillic",
            "مرحبا بالعالم": "arabic",
            "สวัสดีชาวโลก": "thai",
            "1234 !!! 😀": "und",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_language_group(text), expected)

    def test_materially_mixed_text_is_reported(self):
        self.assertEqual(detect_language_group("한국어 hello world"), "mixed")
