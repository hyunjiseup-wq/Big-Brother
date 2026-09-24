"""Retry queue outages must not permanently stop recovery or erase pending work."""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
import bot


class RetryResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_read_failure_restarts_worker_after_delay(self):
        with (
            patch.object(bot.bot, "wait_until_ready", new=AsyncMock()),
            patch.object(bot.database, "get_due_moderation_retries", new=AsyncMock(
                side_effect=[RuntimeError("database unavailable"), asyncio.CancelledError()],
            )) as due,
            patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ai_retry_worker()
        self.assertEqual(due.await_count, 2)
        sleep.assert_awaited_once_with(bot.config.AI_RETRY_POLL_SECONDS)

    async def test_unavailable_guild_keeps_queue_record(self):
        for guild in (None, SimpleNamespace(id=10, unavailable=True)):
            with (
                self.subTest(guild=guild),
                patch.object(bot.bot, "wait_until_ready", new=AsyncMock()) as ready,
                patch.object(bot.database, "get_due_moderation_retries", new=AsyncMock(
                    side_effect=[[(1, 10, 20, 30, 2, "rate_limit")], asyncio.CancelledError()],
                )),
                patch.object(bot.bot, "get_guild", return_value=guild),
                patch.object(bot.database, "delete_moderation_retry", new=AsyncMock()) as delete,
                patch.object(bot.database, "reschedule_moderation_retry", new=AsyncMock()) as reschedule,
                patch.object(bot.asyncio, "sleep", new=AsyncMock()),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await bot.ai_retry_worker()
                delete.assert_not_awaited()
                reschedule.assert_awaited_once_with(
                    1, 3, "discord_guild_unavailable", bot._ai_retry_delay(3),
                )
                self.assertGreaterEqual(ready.await_count, 1)

    async def test_schedule_failure_does_not_kill_worker_or_delete_record(self):
        with (
            patch.object(bot.bot, "wait_until_ready", new=AsyncMock()),
            patch.object(bot.database, "get_due_moderation_retries", new=AsyncMock(
                side_effect=[[(1, 10, 20, 30, 2, "rate_limit")], asyncio.CancelledError()],
            )) as due,
            patch.object(bot.bot, "get_guild", return_value=None),
            patch.object(bot.database, "delete_moderation_retry", new=AsyncMock()) as delete,
            patch.object(bot.database, "reschedule_moderation_retry", new=AsyncMock(
                side_effect=RuntimeError("database locked"),
            )),
            patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ai_retry_worker()
        delete.assert_not_awaited()
        self.assertEqual(due.await_count, 2)
        sleep.assert_awaited_once_with(bot.config.AI_RETRY_POLL_SECONDS)

    async def test_disconnected_worker_waits_without_reading_queue(self):
        with (
            patch.object(bot.bot, "wait_until_ready", new=AsyncMock(
                side_effect=asyncio.CancelledError(),
            )) as ready,
            patch.object(bot.database, "get_due_moderation_retries", new=AsyncMock(
                side_effect=asyncio.CancelledError(),
            )) as due,
            patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ai_retry_worker()
        ready.assert_awaited_once()
        due.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_recovered_database_allows_next_record_to_be_processed(self):
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=SimpleNamespace(
            author=SimpleNamespace(bot=True),
        )))
        guild = SimpleNamespace(get_channel=lambda _id: channel, unavailable=False)
        with (
            patch.object(bot.bot, "wait_until_ready", new=AsyncMock()),
            patch.object(bot.database, "get_due_moderation_retries", new=AsyncMock(
                side_effect=[RuntimeError("temporary"),
                             [(1, 10, 20, 30, 0, "rate_limit")], asyncio.CancelledError()],
            )),
            patch.object(bot.bot, "get_guild", return_value=guild),
            patch.object(bot.database, "delete_moderation_retry", new=AsyncMock()) as delete,
            patch.object(bot.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ai_retry_worker()
        channel.fetch_message.assert_awaited_once_with(30)
        delete.assert_awaited_once_with(1)
        sleep.assert_awaited_once_with(bot.config.AI_RETRY_POLL_SECONDS)

    async def test_readiness_is_checked_between_records(self):
        with (
            patch.object(bot.bot, "wait_until_ready", new=AsyncMock(
                side_effect=[None, None, asyncio.CancelledError()],
            )) as ready,
            patch.object(bot.database, "get_due_moderation_retries", new=AsyncMock(
                return_value=[(1, 10, 20, 30, 0, "rate_limit"),
                              (2, 10, 20, 31, 0, "rate_limit")],
            )),
            patch.object(bot.bot, "get_guild", return_value=None),
            patch.object(bot.database, "reschedule_moderation_retry", new=AsyncMock()) as reschedule,
            patch.object(bot.database, "delete_moderation_retry", new=AsyncMock()) as delete,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ai_retry_worker()
        self.assertEqual(ready.await_count, 3)
        reschedule.assert_awaited_once_with(1, 1, "discord_guild_unavailable", bot._ai_retry_delay(1))
        delete.assert_not_awaited()

    async def test_stop_during_backoff_does_not_restart(self):
        with (
            patch.object(bot.bot, "wait_until_ready", new=AsyncMock()),
            patch.object(bot.database, "get_due_moderation_retries", new=AsyncMock(
                side_effect=RuntimeError("temporary"),
            )) as due,
            patch.object(bot.asyncio, "sleep", new=AsyncMock(
                side_effect=asyncio.CancelledError(),
            )),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ai_retry_worker()
        due.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
