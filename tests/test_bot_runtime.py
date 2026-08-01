"""bot.py 핵심 런타임 흐름(조치 결정·점수 반영·AI 장애·큐 누락) 회귀 테스트."""
import os
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


if __name__ == "__main__":
    unittest.main()
