import unittest
from unittest.mock import patch

import config


class ConfigValidationTests(unittest.TestCase):
    def test_user_facing_sanction_messages_are_disabled_by_default(self):
        self.assertFalse(config.USER_SANCTION_DM_ENABLED)
        self.assertFalse(config.MANUAL_REVIEW_USER_NOTICE_ENABLED)
        self.assertFalse(config.PUBLIC_SANCTION_LOG_ENABLED)
        self.assertIn("실제 경고나 제재가 아니며", config.MANUAL_REVIEW_TEST_NOTICE)

    def test_user_message_flags_must_be_boolean(self):
        for name in (
            "USER_SANCTION_DM_ENABLED",
            "MANUAL_REVIEW_USER_NOTICE_ENABLED",
            "PUBLIC_SANCTION_LOG_ENABLED",
        ):
            with self.subTest(name=name), patch.object(config, name, "false"):
                with self.assertRaisesRegex(ValueError, name):
                    config.validate_config()
    def test_default_policy_allows_tarkov_launcher_event_codes(self):
        self.assertIn("타르코프 이벤트 코드 예외", config.SERVER_RULES)
        self.assertIn("출처 링크 없이 코드만 공유", config.SERVER_RULES)
        self.assertIn("추천인/제휴 보상", config.SERVER_RULES)

    def test_barter_policy_distinguishes_game_currency_from_real_payment(self):
        trade_note = config.CHANNEL_CONTEXT_NOTES[1526179570192093314]
        self.assertIn("게임 내 플리마켓", trade_note)
        self.assertIn("달러·루블·유로", trade_note)
        self.assertIn("앞뒤 대화", trade_note)
        self.assertIn("실제 계좌번호", trade_note)
        self.assertIn("개인 DM", trade_note)

    def test_video_share_policy_allows_videos_and_youtube_channels(self):
        video_note = config.CHANNEL_CONTEXT_NOTES[1409874543295856710]
        self.assertIn("유튜브 영상 링크", video_note)
        self.assertIn("유튜브 채널 링크", video_note)
        self.assertIn("본인 채널", video_note)
        self.assertIn("무단 홍보나 광고로 판단하지 마세요", video_note)
        self.assertIn("피싱·악성 링크", video_note)
        self.assertIn("다른 디스코드 서버 초대", video_note)

    def test_invalid_barter_context_settings_are_rejected(self):
        for name, value in (
            ("BARTER_CHANNEL_IDS", [123, "bad"]),
            ("BARTER_CHANNEL_NAMES", (123,)),
            ("BARTER_CONTEXT_MESSAGE_LIMIT", 0),
            ("BARTER_CONTEXT_MAX_CHARS", 0),
        ):
            with self.subTest(name=name), patch.object(config, name, value):
                with self.assertRaisesRegex(ValueError, name):
                    config.validate_config()

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
