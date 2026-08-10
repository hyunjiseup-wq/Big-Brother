import unittest
from unittest.mock import patch

import config


class ConfigValidationTests(unittest.TestCase):
    def test_default_policy_allows_tarkov_launcher_event_codes(self):
        self.assertIn("타르코프 이벤트 코드 예외", config.SERVER_RULES)
        self.assertIn("출처 링크 없이 코드만 공유", config.SERVER_RULES)
        self.assertIn("추천인/제휴 보상", config.SERVER_RULES)

    def test_channel_id_environment_list_parser(self):
        self.assertEqual(config._parse_channel_id_list("123, 456"), [123, 456])
        self.assertEqual(config._parse_channel_id_list(""), [])

    def test_invalid_channel_id_token_is_preserved_for_validation(self):
        self.assertEqual(config._parse_channel_id_list("123,broken"), [123, "broken"])

    def test_current_configuration_is_valid(self):
        config.validate_config()

    def test_cloud_concurrency_must_be_positive_integer(self):
        for name in ("GEMINI_MAX_CONCURRENT_CALLS", "GROQ_MAX_CONCURRENT_CALLS"):
            with self.subTest(name=name), patch.object(config, name, 0):
                with self.assertRaisesRegex(ValueError, name):
                    config.validate_config()

    def test_realtime_provider_order_rejects_duplicates_and_unknowns(self):
        for value in ((), ("ollama", "ollama"), ("unknown", "gemini")):
            with self.subTest(value=value), patch.object(config, "REALTIME_PROVIDER_ORDER", value):
                with self.assertRaisesRegex(ValueError, "REALTIME_PROVIDER_ORDER"):
                    config.validate_config()

    def test_invalid_batch_backend_is_rejected(self):
        with patch.object(config, "BATCH_BACKEND", "unknown"):
            with self.assertRaisesRegex(ValueError, "BATCH_BACKEND"):
                config.validate_config()

    def test_invalid_watched_channel_id_is_rejected(self):
        with patch.object(config, "WATCHED_CHANNEL_IDS", [123, "not-an-id"]):
            with self.assertRaisesRegex(ValueError, "WATCHED_CHANNEL_IDS"):
                config.validate_config()

    def test_invalid_internal_voice_invite_channel_id_is_rejected(self):
        with patch.object(config, "INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS", [123, "bad"]):
            with self.assertRaisesRegex(ValueError, "INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS"):
                config.validate_config()

    def test_timeout_without_positive_duration_is_rejected(self):
        with patch.object(config, "STRIKE_THRESHOLDS", [(1, "TIMEOUT", None)]):
            with self.assertRaisesRegex(ValueError, "TIMEOUT"):
                config.validate_config()

    def test_malformed_threshold_row_reports_configuration_error(self):
        with patch.object(config, "STRIKE_THRESHOLDS", [(1, "WARN")]):
            with self.assertRaisesRegex(ValueError, "STRIKE_THRESHOLDS"):
                config.validate_config()

    def test_negative_content_retention_is_rejected(self):
        with patch.object(config, "VIOLATION_CONTENT_RETENTION_DAYS", -1):
            with self.assertRaisesRegex(ValueError, "VIOLATION_CONTENT_RETENTION_DAYS"):
                config.validate_config()

    def test_negative_report_retention_is_rejected(self):
        with patch.object(config, "REPORT_RETENTION_DAYS", -1):
            with self.assertRaisesRegex(ValueError, "REPORT_RETENTION_DAYS"):
                config.validate_config()

    def test_administrator_permission_policy_must_be_boolean(self):
        with patch.object(config, "ALLOW_ADMINISTRATOR_PERMISSION", "yes"):
            with self.assertRaisesRegex(ValueError, "ALLOW_ADMINISTRATOR_PERMISSION"):
                config.validate_config()

    def test_realtime_ollama_fallback_flag_must_be_boolean(self):
        with patch.object(config, "OLLAMA_REALTIME_FALLBACK", "yes"):
            with self.assertRaisesRegex(ValueError, "OLLAMA_REALTIME_FALLBACK"):
                config.validate_config()

    def test_local_concurrency_must_be_positive(self):
        with patch.object(config, "OLLAMA_MAX_CONCURRENT_CALLS", 0):
            with self.assertRaisesRegex(ValueError, "OLLAMA_MAX_CONCURRENT_CALLS"):
                config.validate_config()

    def test_negative_ollama_cooldown_is_rejected(self):
        with patch.object(config, "OLLAMA_UNAVAILABLE_COOLDOWN_SECONDS", -1):
            with self.assertRaisesRegex(ValueError, "OLLAMA_UNAVAILABLE_COOLDOWN_SECONDS"):
                config.validate_config()
