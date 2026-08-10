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

    async def test_optional_manual_notice_is_polite_and_not_a_warning(self):
        member = SimpleNamespace(send=AsyncMock())
        with patch.object(bot.config, "MANUAL_REVIEW_USER_NOTICE_ENABLED", True):
            self.assertTrue(await bot._send_manual_review_test_notice(member, "테스트 서버"))
        sent = member.send.await_args.args[0]
        self.assertIn("실제 경고나 제재가 아니며", sent)
        self.assertIn("불이익도 적용되지 않습니다", sent)


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
