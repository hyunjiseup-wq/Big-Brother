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

    def test_default_policy_allows_tarkov_information_sites(self):
        self.assertIn("타르코프 정보 사이트 예외", config.SERVER_RULES)
        self.assertIn("퀘스트·맵·아이템·탄약·시세", config.SERVER_RULES)
        self.assertIn("공식 사이트가 아니거나 작성자 본인 사이트", config.SERVER_RULES)
        self.assertTrue(config.TARKOV_INFO_LINK_EXEMPTION_ENABLED)
        self.assertIn("tarkov.dev", config.TARKOV_INFO_SITE_DOMAINS)
        self.assertIn("mapgenie.io", config.TARKOV_INFO_SITE_PATH_PREFIXES)

    def test_default_policy_requires_korean_split_utterance_reconstruction(self):
        self.assertIn("한국어 분할 발화 판단", config.SERVER_RULES)
        self.assertIn("시발점이 어디예요?", config.SERVER_RULES)
        self.assertIn("결합한 전체 발화가 명백한 모욕", config.SERVER_RULES)

    def test_default_policy_allows_agreed_casual_polite_styles(self):
        self.assertIn("용용체·음슴체·경미한 경어", config.SERVER_RULES)
        self.assertIn("확인했음", config.SERVER_RULES)
        self.assertIn("뭐함", config.SERVER_RULES)
        self.assertIn("실제 모욕·욕설·시비", config.SERVER_RULES)

    def test_default_policy_treats_bdbd_as_context_dependent(self):
        self.assertIn('"ㅂㄷㅂㄷ"', config.SERVER_RULES)
        self.assertIn("부들부들", config.SERVER_RULES)
        self.assertIn("앞뒤 대화", config.SERVER_RULES)

    def test_sherpa_lobby_allows_same_guild_training_channel_guidance(self):
        lobby_id = 1442471746660995113
        note = config.CHANNEL_CONTEXT_NOTES[lobby_id]
        self.assertIn("신규 이용자를 가르치는 선생님", note)
        self.assertIn("교육용 음성채널", note)
        self.assertIn("안내·유도", note)
        self.assertIn("다른 Discord 서버", note)
        self.assertIn(lobby_id, config.INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS)
        self.assertIn(lobby_id, config.WATCHED_CHANNEL_IDS)

    def test_barter_policy_distinguishes_game_currency_from_real_payment(self):
        trade_note = config.CHANNEL_CONTEXT_NOTES[1526179570192093314]
        self.assertIn("게임 내 플리마켓", trade_note)
        self.assertIn("달러·루블·유로", trade_note)
        self.assertIn("앞뒤 대화", trade_note)
        self.assertIn("오버롤 인증 게시판", trade_note)
        self.assertIn("게시글 안에서 공개적으로 진행", trade_note)
        self.assertIn("개인 DM으로 연락해 달라", trade_note)
        self.assertIn("현금·상품권·계좌", trade_note)
        self.assertIn("인게임 거래를 중개하거나 보증하지 않습니다", trade_note)
        self.assertEqual(
            config.BARTER_VERIFICATION_CHANNEL_URL_PREFIXES,
            ("https://discord.com/channels/719020590341685258/1445045515971592294",),
        )

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
            ("BARTER_VERIFICATION_CHANNEL_URL_PREFIXES", ("https://example.com",)),
        ):
            with self.subTest(name=name), patch.object(config, name, value):
                with self.assertRaisesRegex(ValueError, name):
                    config.validate_config()

    def test_invalid_split_message_context_settings_are_rejected(self):
        for name, value in (
            ("SPLIT_MESSAGE_CONTEXT_ENABLED", "true"),
            ("SPLIT_MESSAGE_SETTLE_SECONDS", -1),
            ("SPLIT_MESSAGE_WINDOW_SECONDS", 0),
            ("SPLIT_MESSAGE_MAX_MESSAGES", 1),
            ("SPLIT_MESSAGE_MAX_CHARS", 0),
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

    def test_kpi_schedule_and_sync_settings_are_validated(self):
        with patch.object(config, "KPI_REPORT_HOUR_KST", 24):
            with self.assertRaisesRegex(ValueError, "KPI_REPORT_HOUR_KST"):
                config.validate_config()
        with patch.object(config, "KPI_SYNC_BATCH_SIZE", 0):
            with self.assertRaisesRegex(ValueError, "KPI_SYNC_BATCH_SIZE"):
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

    def test_default_audit_targets_include_special_operation_channels(self):
        expected = {
            1410654534769840209,  # 팀원찾기
            1526179570192093314,  # 물물교환
            1409874543295856710,  # 영상공유
            1445049743150415923,  # 핵 의심 신고
            1445045515971592294,  # 오버롤 인증 게시판
        }
        self.assertTrue(expected.issubset(set(config.WATCHED_CHANNEL_IDS)))

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

    def test_ai_retry_and_ollama_startup_settings_are_validated(self):
        invalid = (
            ("AI_RETRY_ENABLED", "yes"),
            ("AI_RETRY_INITIAL_DELAY_SECONDS", 0),
            ("AI_RETRY_MAX_DELAY_SECONDS", 0),
            ("AI_RETRY_POLL_SECONDS", 0),
            ("AI_RETRY_BATCH_SIZE", 0),
            ("CLOUD_RATE_LIMIT_COOLDOWN_SECONDS", 0),
            ("OLLAMA_AUTO_START", "yes"),
            ("OLLAMA_STARTUP_TIMEOUT_SECONDS", 0),
        )
        for name, value in invalid:
            with self.subTest(name=name), patch.object(config, name, value):
                with self.assertRaisesRegex(ValueError, name):
                    config.validate_config()

        with (
            patch.object(config, "AI_RETRY_INITIAL_DELAY_SECONDS", 60),
            patch.object(config, "AI_RETRY_MAX_DELAY_SECONDS", 30),
        ):
            with self.assertRaisesRegex(ValueError, "초기 지연"):
                config.validate_config()

    def test_local_concurrency_must_be_positive(self):
        with patch.object(config, "OLLAMA_MAX_CONCURRENT_CALLS", 0):
            with self.assertRaisesRegex(ValueError, "OLLAMA_MAX_CONCURRENT_CALLS"):
                config.validate_config()

    def test_negative_ollama_cooldown_is_rejected(self):
        with patch.object(config, "OLLAMA_UNAVAILABLE_COOLDOWN_SECONDS", -1):
            with self.assertRaisesRegex(ValueError, "OLLAMA_UNAVAILABLE_COOLDOWN_SECONDS"):
                config.validate_config()
