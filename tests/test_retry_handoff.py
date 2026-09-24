"""Real SQLite rollback/concurrency tests for durable retry -> manual review handoff."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import database
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
import bot


class RetryHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp.name, "handoff.db")
        await database.init_db()
        await database.enqueue_moderation_retry(1, 10, 100, "offline", 0)
        self.retry_id = await database.get_moderation_retry_id(1, 100)

    async def asyncTearDown(self):
        database.DB_PATH = self.old_path
        self.temp.cleanup()

    async def handoff(self, **kwargs):
        return await database.create_review_record(
            1, 5, 10, 100, "evidence", "MODERATE", "reason", "manual review",
            retry_id=kwargs.get("retry_id", self.retry_id),
        )

    async def counts(self):
        async with aiosqlite.connect(database.DB_PATH) as db:
            counts = []
            for table in ("moderation_retry_queue", "violation_log", "kpi_sync_outbox"):
                counts.append((await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0])
            return tuple(counts)

    async def test_atomic_handoff_and_repeated_call(self):
        record = await self.handoff()
        self.assertIsInstance(record, int)
        self.assertIsNone(await self.handoff())
        self.assertEqual(await self.counts(), (0, 1, 1))
        async with aiosqlite.connect(database.DB_PATH) as db:
            row = await (await db.execute(
                "SELECT retry_queue_id, card_delivered, review_status FROM violation_log"
            )).fetchone()
        self.assertEqual(row, (self.retry_id, 0, "pending"))
        self.assertEqual(len(await database.get_reviews_without_card(1)), 1)
        await database.mark_review_delivered(record, 1)
        self.assertEqual(await database.get_reviews_without_card(1), [])

    async def test_concurrent_duplicate_creates_only_one_review(self):
        results = await asyncio.gather(self.handoff(), self.handoff(), self.handoff())
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(await self.counts(), (0, 1, 1))

    async def test_outbox_failure_rolls_back_review_and_keeps_queue(self):
        with patch.object(database, "_enqueue_kpi_sync", new=AsyncMock(side_effect=RuntimeError("disk"))):
            with self.assertRaises(RuntimeError):
                await self.handoff()
        self.assertEqual(await self.counts(), (1, 0, 0))
        await self.handoff()
        self.assertEqual(await self.counts(), (0, 1, 1))

    async def test_delete_failure_rolls_back_even_after_review_and_outbox_insert(self):
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON moderation_retry_queue "
                             "BEGIN SELECT RAISE(ABORT, 'injected'); END")
            await db.commit()
        with self.assertRaises(aiosqlite.IntegrityError):
            await self.handoff()
        self.assertEqual(await self.counts(), (1, 0, 0))

    async def test_cancellation_rolls_back(self):
        with patch.object(database, "_enqueue_kpi_sync", new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.handoff()
        self.assertEqual(await self.counts(), (1, 0, 0))

    async def test_wrong_queue_identity_is_rejected(self):
        await database.enqueue_moderation_retry(2, 10, 100, "offline", 0)
        other = await database.get_moderation_retry_id(2, 100)
        with self.assertRaises(ValueError):
            await self.handoff(retry_id=other)
        self.assertEqual(await self.counts(), (2, 0, 0))

    async def test_new_queue_generation_is_not_acknowledged_by_old_worker(self):
        await self.handoff()
        await database.enqueue_moderation_retry(1, 10, 100, "offline again", 0)
        new_id = await database.get_moderation_retry_id(1, 100)
        self.assertNotEqual(self.retry_id, new_id)
        self.assertIsNone(await self.handoff())
        await database.delete_moderation_retry(self.retry_id)
        self.assertEqual(await database.get_moderation_retry_id(1, 100), new_id)

    def message(self):
        message = SimpleNamespace(
            id=100, content="evidence", attachments=[],
            guild=SimpleNamespace(id=1, name="test"),
            channel=SimpleNamespace(id=10, mention="#test", name="test"),
            author=SimpleNamespace(id=5, mention="user"),
            created_at=datetime.now(timezone.utc), jump_url="https://discord.com/channels/1/10/100",
        )
        message.channel.fetch_message = AsyncMock(return_value=message)
        return message

    async def test_card_exception_after_commit_preserves_unposted_review(self):
        with (
            patch.object(bot, "send_log", new=AsyncMock(side_effect=RuntimeError("network"))),
            patch.object(bot, "_build_review_view"),
            patch.object(bot.config, "MANUAL_REVIEW_USER_NOTICE_ENABLED", False),
        ):
            with self.assertRaises(RuntimeError):
                await bot._handle_violation_review_only(
                    self.message(), "MODERATE", "reason", "3", "test", 1, retry_id=self.retry_id,
                )
        self.assertEqual(await self.counts(), (0, 1, 1))
        self.assertEqual(len(await database.get_reviews_without_card(1)), 1)
        with patch.object(bot, "send_log", new=AsyncMock()) as send:
            await bot._handle_violation_review_only(
                self.message(), "MODERATE", "reason", "3", "test", 1, retry_id=self.retry_id,
            )
        send.assert_not_awaited()

    async def test_retry_never_auto_sanctions_even_if_mode_changes(self):
        with (
            patch.object(bot.learning, "is_known_false_positive", new=AsyncMock(return_value=False)),
            patch.object(bot.config, "MANUAL_REVIEW_MODE", False),
            patch.object(bot, "_handle_violation_review_only", new=AsyncMock()) as review,
            patch.object(bot, "apply_action", new=AsyncMock()) as action,
        ):
            await bot.handle_violation(self.message(), "MODERATE", "reason", retry_id=self.retry_id)
        action.assert_not_awaited()
        self.assertEqual(review.await_args.kwargs["retry_id"], self.retry_id)

    async def test_changed_message_remains_queued_for_reclassification(self):
        message = self.message()
        message.channel.fetch_message.return_value = SimpleNamespace(content="edited", attachments=[])
        with self.assertRaises(RuntimeError):
            await bot.handle_violation(message, "MODERATE", "reason", retry_id=self.retry_id)
        self.assertEqual(await self.counts(), (1, 0, 0))

    async def test_realtime_handoff_does_not_remove_queue_on_save_failure(self):
        with patch.object(bot, "handle_violation", new=AsyncMock(side_effect=RuntimeError("disk"))) as handle:
            with self.assertRaises(RuntimeError):
                await bot._handle_ai_verdict(self.message(), "MODERATE", "reason")
        self.assertEqual(handle.await_args.kwargs["retry_id"], self.retry_id)
        self.assertEqual(await self.counts(), (1, 0, 0))

    async def test_provider_failure_never_completes_existing_retry(self):
        with patch.object(bot, "handle_violation", new=AsyncMock()) as handle:
            await bot._handle_ai_verdict(self.message(), "NONE", "outage", provider="none")
        handle.assert_not_awaited()
        self.assertEqual(await self.counts(), (1, 0, 0))


if __name__ == "__main__":
    unittest.main()
