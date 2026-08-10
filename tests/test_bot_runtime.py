"""bot.py 핵심 런타임 흐름(조치 결정·점수 반영·AI 장애·큐 누락) 회귀 테스트."""
import os
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# bot.py는 임포트 시 .env의 토큰을 요구한다. 테스트에서는 최소값만 채워 준다.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import bot  # noqa: E402
import database  # noqa: E402
import learning  # noqa: E402


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_valid_minimum_environment(self):
        values = {
            "DISCORD_BOT_TOKEN": "real-looking-test-token",
            "GEMINI_API_KEY": "gemini-test-key",
            "GROQ_API_KEY": "",
            "LOG_CHANNEL_ID": "123456789",
            "PUBLIC_LOG_CHANNEL_ID": "",
            "REPORT_CHANNEL_ID": "",
        }
        with patch.dict(os.environ, values, clear=True):
            bot.validate_runtime_environment()

    def test_missing_required_environment_is_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DISCORD_BOT_TOKEN"):
                bot.validate_runtime_environment()


class PermissionWarningTests(unittest.TestCase):
    def test_administrator_permission_is_flagged(self):
        permissions = SimpleNamespace(
            administrator=True,
            manage_messages=True,
            moderate_members=True,
            kick_members=True,
            ban_members=True,
        )
        with patch.object(bot.config, "ALLOW_ADMINISTRATOR_PERMISSION", False):
            warnings = bot.permission_warnings(SimpleNamespace(guild_permissions=permissions))
        self.assertTrue(any("Administrator" in warning for warning in warnings))

    def test_explicitly_allowed_administrator_has_no_warning(self):
        permissions = SimpleNamespace(
            administrator=True,
            manage_messages=True,
            moderate_members=True,
            kick_members=True,
            ban_members=True,
        )
        with patch.object(bot.config, "ALLOW_ADMINISTRATOR_PERMISSION", True):
            warnings = bot.permission_warnings(SimpleNamespace(guild_permissions=permissions))
        self.assertEqual(warnings, [])

    def test_missing_required_permissions_are_listed(self):
        permissions = SimpleNamespace(
            administrator=False,
            manage_messages=True,
            moderate_members=False,
            kick_members=False,
            ban_members=True,
        )
        warnings = bot.permission_warnings(SimpleNamespace(guild_permissions=permissions))
        self.assertTrue(any("멤버 타임아웃" in warning and "멤버 추방" in warning for warning in warnings))

    def test_least_privilege_configuration_has_no_warning(self):
        permissions = SimpleNamespace(
            administrator=False,
            manage_messages=True,
            moderate_members=True,
            kick_members=True,
            ban_members=True,
        )
        self.assertEqual(
            bot.permission_warnings(SimpleNamespace(guild_permissions=permissions)), []
        )

    def test_invalid_optional_channel_id_is_rejected(self):
        values = {
            "DISCORD_BOT_TOKEN": "real-looking-test-token",
            "GEMINI_API_KEY": "gemini-test-key",
            "LOG_CHANNEL_ID": "123456789",
            "REPORT_CHANNEL_ID": "not-a-channel",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(RuntimeError, "REPORT_CHANNEL_ID"):
                bot.validate_runtime_environment()


class InternalVoiceInviteTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def message(source_channel_id=10, guild_id=1):
        return SimpleNamespace(
            content="같이 하실 분 https://discord.gg/team123",
            guild=SimpleNamespace(id=guild_id),
            channel=SimpleNamespace(id=source_channel_id),
            author=SimpleNamespace(id=50),
        )

    async def test_same_guild_voice_invite_is_not_fast_blocked(self):
        invite = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(type=bot.discord.ChannelType.voice),
        )
        with (
            patch.object(bot.config, "INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS", [10]),
            patch.object(bot.bot, "fetch_invite", new=AsyncMock(return_value=invite)),
        ):
            result = await bot._fast_check_with_invite_context(self.message())
        self.assertEqual(result.decision, "NEEDS_AI")

    async def test_other_guild_invite_is_blocked(self):
        invite = SimpleNamespace(
            guild=SimpleNamespace(id=999),
            channel=SimpleNamespace(type=bot.discord.ChannelType.voice),
        )
        with (
            patch.object(bot.config, "INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS", [10]),
            patch.object(bot.bot, "fetch_invite", new=AsyncMock(return_value=invite)),
        ):
            result = await bot._fast_check_with_invite_context(self.message())
        self.assertEqual((result.decision, result.level), ("DECIDED", "MODERATE"))
        self.assertIn("다른", result.reason)

    async def test_same_guild_text_channel_invite_is_blocked(self):
        invite = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(type=bot.discord.ChannelType.text),
        )
        with (
            patch.object(bot.config, "INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS", [10]),
            patch.object(bot.bot, "fetch_invite", new=AsyncMock(return_value=invite)),
        ):
            result = await bot._fast_check_with_invite_context(self.message())
        self.assertEqual((result.decision, result.level), ("DECIDED", "MODERATE"))
        self.assertIn("음성채널", result.reason)

    async def test_many_invites_are_blocked_without_api_fanout(self):
        message = self.message()
        message.content = " ".join(f"https://discord.gg/code{i}" for i in range(4))
        with (
            patch.object(bot.config, "INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS", [10]),
            patch.object(bot.bot, "fetch_invite", new=AsyncMock()) as fetch,
        ):
            result = await bot._fast_check_with_invite_context(message)
        fetch.assert_not_awaited()
        self.assertEqual((result.decision, result.level), ("DECIDED", "MODERATE"))
        self.assertIn("과다", result.reason)

    async def test_invite_outside_team_finder_is_blocked_without_api_call(self):
        with (
            patch.object(bot.config, "INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS", [10]),
            patch.object(bot.bot, "fetch_invite", new=AsyncMock()) as fetch,
        ):
            result = await bot._fast_check_with_invite_context(self.message(source_channel_id=20))
        fetch.assert_not_awaited()
        self.assertEqual((result.decision, result.level), ("DECIDED", "MODERATE"))


class BarterConversationContextTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def message(content="네 맞아요", channel_id=10, user_id=50, history_items=None):
        items = history_items or []

        async def history(**kwargs):
            for item in items:
                yield item

        channel = SimpleNamespace(
            id=channel_id,
            parent_id=None,
            parent=None,
            name="물물교환",
            history=history,
        )
        return SimpleNamespace(
            id=999,
            content=content,
            channel=channel,
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=user_id),
        )

    async def test_short_dm_invitation_is_sent_to_ai_in_barter_channel(self):
        message = self.message(content="디엠")
        with patch.object(bot.config, "BARTER_CHANNEL_IDS", [10]):
            result = await bot._fast_check_with_invite_context(message)
        self.assertEqual(result.decision, "NEEDS_AI")

    async def test_short_game_currency_text_is_not_treated_as_external_trade(self):
        message = self.message(content="1원")
        with patch.object(bot.config, "BARTER_CHANNEL_IDS", [10]):
            result = await bot._fast_check_with_invite_context(message)
        self.assertEqual(result.decision, "SKIP")

    async def test_barter_history_is_chronological_and_anonymized(self):
        # Discord history(oldest_first=False)는 최신 메시지부터 반환한다.
        items = [
            SimpleNamespace(
                content="10만원 맞나요?", author=SimpleNamespace(id=50, bot=False)
            ),
            SimpleNamespace(
                content="플리마켓에 올릴게요", author=SimpleNamespace(id=60, bot=False)
            ),
        ]
        message = self.message(history_items=items)
        with patch.object(bot.config, "BARTER_CHANNEL_IDS", [10]):
            context = await bot._barter_conversation_context(message)
        self.assertEqual(
            context,
            [
                {"speaker": "other_user_1", "content": "플리마켓에 올릴게요"},
                {"speaker": "current_user", "content": "10만원 맞나요?"},
            ],
        )
        self.assertNotIn("50", str(context))
        self.assertNotIn("60", str(context))

    async def test_thread_inherits_barter_parent_identity(self):
        channel = SimpleNamespace(
            id=20,
            parent_id=10,
            parent=SimpleNamespace(id=10, name="물물교환"),
            name="그래픽카드 교환",
        )
        with patch.object(bot.config, "BARTER_CHANNEL_IDS", [10]):
            self.assertTrue(bot._is_barter_channel(channel))


def _message(content="bad text", guild_id=1, user_id=50, message_id=999):
    channel = SimpleNamespace(id=10, mention="#general")
    message = SimpleNamespace(
        id=message_id,
        content=content,
        channel=channel,
        guild=SimpleNamespace(id=guild_id, name="guild"),
        author=SimpleNamespace(id=user_id, mention=f"<@{user_id}>"),
        jump_url="http://example/1",
    )
    # handle_violation은 제재 직전 원문을 다시 확인한다 (수정/삭제 방지).
    channel.fetch_message = AsyncMock(return_value=message)
    return message


class DetermineActionTests(unittest.TestCase):
    def test_thresholds_escalate_with_points(self):
        self.assertEqual(bot.determine_action(0, "MINOR")[0], "NONE")
        self.assertEqual(bot.determine_action(1, "MINOR")[0], "WARN")
        self.assertEqual(bot.determine_action(3, "MODERATE")[0], "DELETE")
        self.assertEqual(bot.determine_action(6, "MODERATE")[:2], ("TIMEOUT", 60))

    def test_kick_and_ban_are_downgraded_for_admin_review(self):
        for points in (20, 30):
            action, duration, downgraded = bot.determine_action(points, "MODERATE")
            self.assertEqual(action, bot.config.AUTO_ACTION_CEILING)
            self.assertTrue(downgraded)
            self.assertEqual(duration, bot.config.AUTO_ACTION_CEILING_TIMEOUT_MINUTES)


class AutoModePointsTests(unittest.IsolatedAsyncioTestCase):
    """조치가 실제로 집행됐을 때만 누적 점수가 오르는지 확인한다."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        learning._reset_for_tests()
        await database.init_db()
        self._patchers = [
            patch.object(bot.config, "MANUAL_REVIEW_MODE", False),
            patch.object(bot, "send_log", new=AsyncMock(return_value=True)),
            patch.object(bot, "send_public_sanction_log", new=AsyncMock()),
            patch.object(bot.learning, "is_known_false_positive", new=AsyncMock(return_value=False)),
        ]
        for p in self._patchers:
            p.start()

    async def asyncTearDown(self):
        for p in self._patchers:
            p.stop()
        learning._reset_for_tests()
        self.temp_dir.cleanup()

    async def test_points_are_added_when_action_succeeds(self):
        message = _message()
        with patch.object(bot, "apply_action", new=AsyncMock(return_value=(True, "ok"))):
            await bot.handle_violation(message, "MODERATE", "reason")
        self.assertEqual(await database.get_points(1, 50), 3)

    async def test_points_are_not_added_when_action_fails(self):
        """권한 부족 등으로 제재가 실패했는데 점수만 오르면 다음 위반이 과잉 처벌된다."""
        message = _message()
        with patch.object(bot, "apply_action", new=AsyncMock(return_value=(False, "권한 없음"))):
            await bot.handle_violation(message, "MODERATE", "reason")
        self.assertEqual(await database.get_points(1, 50), 0)

    async def test_failed_action_is_recorded_as_failed(self):
        message = _message()
        with patch.object(bot, "apply_action", new=AsyncMock(return_value=(False, "권한 없음"))):
            await bot.handle_violation(message, "SEVERE", "reason")
        rows = await database.get_recent_violations(1, 50)
        self.assertTrue(rows[0][2].endswith("_FAILED"))

    async def test_warn_succeeds_without_sending_user_dm_when_disabled(self):
        message = _message()
        message.author.send = AsyncMock()
        with patch.object(bot.config, "USER_SANCTION_DM_ENABLED", False):
            ok, detail = await bot.apply_action(message, "WARN", None, "reason")
        self.assertTrue(ok)
        message.author.send.assert_not_awaited()
        self.assertIn("DM 비활성화", detail)

    async def test_enabled_sanction_dm_contains_message_evidence(self):
        message = _message(content="제가 작성한 문제 메시지")
        message.created_at = bot.datetime.datetime(
            2026, 8, 10, 12, 34, tzinfo=bot.datetime.timezone.utc
        )
        message.author.send = AsyncMock()
        with patch.object(bot.config, "USER_SANCTION_DM_ENABLED", True):
            ok, detail = await bot.apply_action(message, "WARN", None, "관리자 확인 사유")
        self.assertTrue(ok)
        self.assertIn("DM 성공", detail)
        sent = message.author.send.await_args.args[0]
        self.assertIn("제가 작성한 문제 메시지", sent)
        self.assertIn("#general", sent)
        self.assertIn("작성 시각", sent)
        self.assertIn(message.jump_url, sent)
        self.assertIn("이의 제기", sent)
        allowed_mentions = message.author.send.await_args.kwargs["allowed_mentions"]
        self.assertFalse(allowed_mentions.everyone)

    async def test_failed_primary_action_does_not_send_false_success_dm(self):
        message = _message(content="제재 대상 메시지")
        message.created_at = bot.datetime.datetime.now(bot.datetime.timezone.utc)
        message.author.send = AsyncMock()
        response = SimpleNamespace(status=403, reason="Forbidden")
        message.author.timeout = AsyncMock(
            side_effect=bot.discord.Forbidden(response, "권한 부족")
        )
        message.delete = AsyncMock()

        with patch.object(bot.config, "USER_SANCTION_DM_ENABLED", True):
            ok, detail = await bot.apply_action(message, "TIMEOUT", 60, "관리자 확인 사유")

        self.assertFalse(ok)
        self.assertIn("핵심 조치 실패로 사용자 DM 미전송", detail)
        message.author.send.assert_not_awaited()

    async def test_kick_uses_dm_channel_prepared_before_removal(self):
        message = _message(content="제재 대상 메시지")
        message.created_at = bot.datetime.datetime.now(bot.datetime.timezone.utc)
        message.delete = AsyncMock()
        message.author.kick = AsyncMock()
        message.author.send = AsyncMock()
        dm_channel = SimpleNamespace(send=AsyncMock())
        message.author.create_dm = AsyncMock(return_value=dm_channel)

        with patch.object(bot.config, "USER_SANCTION_DM_ENABLED", True):
            ok, detail = await bot.apply_action(message, "KICK", None, "관리자 확인 사유")

        self.assertTrue(ok)
        self.assertIn("DM 성공", detail)
        message.author.create_dm.assert_awaited_once()
        dm_channel.send.assert_awaited_once()
        message.author.send.assert_not_awaited()

    async def test_optional_manual_notice_is_polite_and_not_a_warning(self):
        member = SimpleNamespace(send=AsyncMock())
        with patch.object(bot.config, "MANUAL_REVIEW_USER_NOTICE_ENABLED", True):
            self.assertTrue(await bot._send_manual_review_test_notice(member, "테스트 서버"))
        sent = member.send.await_args.args[0]
        self.assertIn("실제 경고나 제재가 아니며", sent)
        self.assertIn("불이익도 적용되지 않습니다", sent)


class SanctionNoticeTests(unittest.TestCase):
    def test_deleted_message_evidence_is_restored_from_ids(self):
        created_at = bot.datetime.datetime(
            2026, 8, 10, 9, 15, tzinfo=bot.datetime.timezone.utc
        )
        message_id = bot.discord.utils.time_snowflake(created_at)
        notice = bot._build_user_sanction_notice(
            "한국 타르코프", "60분 타임아웃", "관리자 수동 검수 확정",
            guild_id=123, channel_id=456, message_id=message_id,
            message_content="삭제 전 보존된 원문", channel_display="#팀원찾기",
        )
        self.assertIn("삭제 전 보존된 원문", notice)
        self.assertIn("#팀원찾기 (ID: `456`)", notice)
        self.assertIn(
            f"https://discord.com/channels/123/456/{message_id}", notice
        )
        self.assertIn(f"<t:{int(created_at.timestamp())}:F>", notice)
        self.assertLessEqual(len(notice), 2000)


class ManualReviewSanctionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_warning_uses_full_stored_evidence(self):
        created_at = bot.datetime.datetime(
            2026, 8, 10, 11, 20, tzinfo=bot.datetime.timezone.utc
        )
        target_message = SimpleNamespace(
            content="DB와 Discord에서 확인한 전체 원문",
            created_at=created_at,
            jump_url="https://discord.com/channels/1/10/999",
            delete=AsyncMock(),
        )
        channel = SimpleNamespace(
            id=10, mention="#검수대상", name="검수대상",
            fetch_message=AsyncMock(return_value=target_message),
        )
        member = SimpleNamespace(send=AsyncMock())
        guild = SimpleNamespace(id=1, name="테스트 서버")
        guild.get_member = lambda user_id: member
        guild.get_channel = lambda channel_id: channel
        guild.get_channel_or_thread = lambda channel_id: channel
        embed = bot.discord.Embed()
        embed.add_field(name="위반 등급", value="MODERATE")
        embed.add_field(name="위반 규정", value="외부 광고")
        embed.add_field(name="사유", value="관리자가 문맥을 확인함")
        embed.add_field(name="원문", value="절단된 원문")
        log_message = SimpleNamespace(embeds=[embed])
        admin = SimpleNamespace(id=77, mention="<@77>")

        with (
            patch.object(bot.config, "USER_SANCTION_DM_ENABLED", True),
            patch.object(bot.database, "claim_review", new=AsyncMock(return_value=True)),
            patch.object(
                bot.database, "get_violation_content",
                new=AsyncMock(return_value="DB에 보존된 전체 원문"),
            ),
            patch.object(bot.database, "resolve_review", new=AsyncMock()),
            patch.object(bot.database, "add_points", new=AsyncMock(return_value=3)),
            patch.object(bot, "send_public_sanction_log", new=AsyncMock()),
        ):
            ok, result = await bot._apply_review_action(
                guild, log_message, admin, "warn", 10, 999, 50, review_id=123
            )

        self.assertTrue(ok)
        self.assertIn("경고 전달", result)
        notice = member.send.await_args.args[0]
        self.assertIn("DB와 Discord에서 확인한 전체 원문", notice)
        self.assertIn("#검수대상", notice)
        self.assertIn(f"<t:{int(created_at.timestamp())}:F>", notice)
        target_message.delete.assert_not_awaited()


class AiOutageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot._ai_outage_state.clear()
        self.send_log = AsyncMock(return_value=True)
        self.patcher = patch.object(bot, "send_log", new=self.send_log)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        bot._ai_outage_state.clear()

    @staticmethod
    def _result(provider):
        return SimpleNamespace(provider=provider)

    async def test_failure_streak_is_tracked_per_guild(self):
        """한 서버의 성공이 다른 서버의 연속 실패를 초기화하면 안 된다."""
        guild_a = SimpleNamespace(id=1)
        guild_b = SimpleNamespace(id=2)

        for _ in range(3):
            await bot._track_ai_outage(guild_a, self._result("none"))
        await bot._track_ai_outage(guild_b, self._result("gemini"))

        self.assertEqual(bot._ai_outage_state[1]["streak"], 3)
        self.assertEqual(bot._ai_outage_state[2]["streak"], 0)

    async def test_alert_fires_once_threshold_reached(self):
        guild = SimpleNamespace(id=1)
        for _ in range(bot.config.AI_OUTAGE_ALERT_THRESHOLD):
            await bot._track_ai_outage(guild, self._result("none"))
        self.assertEqual(self.send_log.await_count, 1)

        # 쿨다운 동안에는 반복 경고하지 않는다.
        await bot._track_ai_outage(guild, self._result("none"))
        self.assertEqual(self.send_log.await_count, 1)

    async def test_recovery_resets_streak(self):
        guild = SimpleNamespace(id=1)
        await bot._track_ai_outage(guild, self._result("none"))
        await bot._track_ai_outage(guild, self._result("gemini"))
        self.assertEqual(bot._ai_outage_state[1]["streak"], 0)

    async def test_total_ai_failure_is_deferred_instead_of_treated_as_none(self):
        message = _message()
        result = SimpleNamespace(provider="none", failure_category="gemini:rate_limit")
        with (
            patch.object(bot.config, "AI_RETRY_ENABLED", True),
            patch.object(bot.database, "enqueue_moderation_retry", new=AsyncMock()) as enqueue,
        ):
            deferred = await bot._defer_ai_failure(message, result, "test")
        self.assertTrue(deferred)
        enqueue.assert_awaited_once_with(
            message.guild.id,
            message.channel.id,
            message.id,
            "gemini:rate_limit",
            bot.config.AI_RETRY_INITIAL_DELAY_SECONDS,
        )

    def test_retry_delay_is_bounded(self):
        self.assertEqual(bot._ai_retry_delay(0), bot.config.AI_RETRY_INITIAL_DELAY_SECONDS)
        self.assertLessEqual(bot._ai_retry_delay(100), bot.config.AI_RETRY_MAX_DELAY_SECONDS)


class AiRetryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_message_is_reclassified_and_removed_after_recovery(self):
        message = _message(content="재검사 대상")
        message.author.bot = False
        message.guild.get_channel = lambda channel_id: message.channel
        verdict = bot.moderator.ModerationResult(
            "MODERATE", "3", "재검사에서 위반 확인", provider="ollama"
        )
        due_rows = [[(7, message.guild.id, message.channel.id, message.id, 0, "rate_limit")]]
        with (
            patch.object(
                bot.database, "get_due_moderation_retries",
                new=AsyncMock(side_effect=due_rows + [asyncio.CancelledError()]),
            ),
            patch.object(bot.bot, "get_guild", return_value=message.guild),
            patch.object(bot.learning, "is_known_false_positive",
                         new=AsyncMock(return_value=False)),
            patch.object(bot.learning, "get_prompt_examples",
                         new=AsyncMock(return_value=[])),
            patch.object(bot, "classify_message", new=AsyncMock(return_value=verdict)) as classify,
            patch.object(bot, "_track_ai_outage", new=AsyncMock()),
            patch.object(bot.database, "delete_moderation_retry", new=AsyncMock()) as delete,
            patch.object(bot, "handle_violation", new=AsyncMock()) as handle,
            patch.object(bot.cache, "get", return_value=None),
            patch.object(bot.cache, "set"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ai_retry_worker()
        classify.assert_awaited_once()
        delete.assert_awaited_once_with(7)
        handle.assert_awaited_once()
        self.assertEqual(handle.await_args.args[1], "MODERATE")


class DropAlertTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot._dropped_count = 0
        bot._expired_count = 0
        bot._last_drop_at = None
        bot._last_drop_alert_at = None

    def tearDown(self):
        bot._dropped_count = 0
        bot._expired_count = 0

    async def test_drop_counts_are_tracked_separately(self):
        guild = SimpleNamespace(id=1)
        with patch.object(bot, "_spawn"):
            bot._note_drop(guild)
            bot._note_drop(guild, expired=True)
        self.assertEqual((bot._dropped_count, bot._expired_count), (1, 1))
        self.assertIsNotNone(bot._last_drop_at)

    async def test_alert_is_raised_after_threshold(self):
        """누락이 임계치를 넘으면 콘솔이 아니라 로그 채널로 경고가 나가야 한다."""
        guild = SimpleNamespace(id=1)
        with patch.object(bot, "_spawn") as spawn:
            for _ in range(bot.config.DROP_ALERT_THRESHOLD):
                bot._note_drop(guild)
        self.assertEqual(spawn.call_count, 1)
        spawn.call_args[0][0].close()  # 실행하지 않은 코루틴 정리

    async def test_alert_respects_cooldown(self):
        guild = SimpleNamespace(id=1)
        send_log = AsyncMock(return_value=True)
        with patch.object(bot, "send_log", new=send_log):
            await bot._alert_drops(guild)
            await bot._alert_drops(guild)
        self.assertEqual(send_log.await_count, 1)


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        bot._background_tasks.clear()
        bot._workers_started = True

    async def asyncTearDown(self):
        for task in tuple(bot._background_tasks):
            task.cancel()
        bot._background_tasks.clear()
        bot._workers_started = False

    async def test_close_cancels_background_workers_before_connections(self):
        started = asyncio.Event()

        async def waiting_worker():
            started.set()
            await asyncio.Event().wait()

        task = bot._spawn(waiting_worker())
        await started.wait()

        with (
            patch.object(bot.batch_audit_task, "is_running", return_value=True),
            patch.object(bot.batch_audit_task, "cancel") as cancel_audit,
            patch.object(bot.moderator, "aclose_http_client", new=AsyncMock()) as close_http,
            patch.object(bot.commands.Bot, "close", new=AsyncMock()) as close_discord,
        ):
            await bot.close()

        cancel_audit.assert_called_once_with()
        self.assertTrue(task.cancelled())
        self.assertFalse(bot._workers_started)
        close_http.assert_awaited_once_with()
        close_discord.assert_awaited_once_with(bot.bot)


if __name__ == "__main__":
    unittest.main()
