"""All-age unposted-review visibility, pagination and administrator-only DM delivery."""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import database

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
import bot


class UncardedQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp.name, "reviews.db")
        await database.init_db()

    async def asyncTearDown(self):
        database.DB_PATH = self.previous_path
        self.temp.cleanup()

    async def record(self, message_id, guild_id=1, *, delivered=False, age_days=100):
        record_id = await database.create_review_record(
            guild_id, 20, 30, message_id, "content", "MODERATE", "reason", "review only",
            card_delivered=delivered,
        )
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute("UPDATE violation_log SET created_at = ? WHERE id = ?",
                             (time.time() - age_days * 86400, record_id))
            await db.commit()
        return record_id

    async def test_no_age_cutoff_and_all_pages_cover_every_record(self):
        ids = [await self.record(i) for i in range(17)]
        # Equal timestamps must still use stable ID ordering.
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute("UPDATE violation_log SET created_at = 100")
            await db.commit()
        collected = []
        for page in range(1, 5):
            result = await database.get_uncarded_review_page(1, page)
            self.assertEqual((result["total"], result["pages"]), (17, 4))
            collected.extend(row[0] for row in result["rows"])
        self.assertEqual(collected, ids)
        self.assertEqual((await database.get_uncarded_review_page(1, 10**30))["rows"], [])

    async def test_excludes_other_guild_delivered_and_nonpending_statuses(self):
        expected = await self.record(1)
        await self.record(2, guild_id=2)
        await self.record(3, delivered=True)
        for index, status in enumerate(("processing", "confirmed", "false_positive", "superseded"), 4):
            record_id = await self.record(index)
            async with aiosqlite.connect(database.DB_PATH) as db:
                await db.execute("UPDATE violation_log SET review_status = ? WHERE id = ?",
                                 (status, record_id))
                await db.commit()
        result = await database.get_uncarded_review_page(1)
        self.assertEqual(result["total"], 1)
        self.assertEqual([row[0] for row in result["rows"]], [expected])

    async def test_query_preserves_state_and_new_index_exists(self):
        await self.record(1)
        async with aiosqlite.connect(database.DB_PATH) as db:
            before = await (await db.execute("SELECT * FROM violation_log")).fetchall()
            plan = await (await db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM violation_log WHERE guild_id = 1 "
                "AND review_status = 'pending' AND card_delivered = 0 ORDER BY created_at,id"
            )).fetchall()
        await database.get_uncarded_review_page(1)
        async with aiosqlite.connect(database.DB_PATH) as db:
            after = await (await db.execute("SELECT * FROM violation_log")).fetchall()
        self.assertEqual(before, after)
        self.assertIn("idx_violation_uncarded_pending", str(plan))

    async def test_empty_and_invalid_pages(self):
        self.assertEqual(await database.get_uncarded_review_page(1),
                         {"total": 0, "pages": 0, "page": 1, "rows": []})
        for page, size in ((0, 5), (-1, 5), (1, 0), (1, 11)):
            with self.assertRaises(ValueError):
                await database.get_uncarded_review_page(1, page, size)


class UncardedCommandTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        return SimpleNamespace(guild=SimpleNamespace(id=1, get_member=lambda _id: None),
                               author=SimpleNamespace(send=AsyncMock()), send=AsyncMock())

    def result(self, total=17, message_id=100):
        return {"total": total, "page": 1, "pages": (total + 4) // 5,
                "rows": [(index, 20, 30, "MODERATE", "private @everyone " + "x" * 3000,
                          100, message_id) for index in range(1, min(5, total) + 1)]}

    async def test_details_go_only_to_staff_dm_with_bounded_embed_and_no_ping(self):
        ctx = self.context()
        with patch.object(database, "get_uncarded_review_page", new=AsyncMock(return_value=self.result())):
            await bot.show_uncarded_reviews.callback(ctx, 1)
        embed = ctx.author.send.await_args.kwargs["embed"]
        self.assertIn("17건", embed.description)
        self.assertIn("!BB 미전송검수 2", embed.footer.text)
        self.assertEqual(len(embed.fields), 5)
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))
        self.assertIn("https://discord.com/channels/1/30/100", embed.fields[0].value)
        self.assertIn("<t:100:F>", embed.fields[0].value)
        self.assertEqual(ctx.author.send.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertNotIn("private", str(ctx.send.await_args))

    async def test_dm_failure_never_falls_back_to_public_details(self):
        ctx = self.context()
        ctx.author.send.side_effect = bot.discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        with patch.object(database, "get_uncarded_review_page", new=AsyncMock(return_value=self.result())):
            await bot.show_uncarded_reviews.callback(ctx, 1)
        self.assertIn("DM을 열 수 없습니다", ctx.send.await_args.args[0])
        self.assertNotIn("private", str(ctx.send.await_args))
        self.assertNotIn("embed", ctx.send.await_args.kwargs)

    async def test_invalid_page_rejected_before_database_and_out_of_range_no_dm(self):
        ctx = self.context()
        with patch.object(database, "get_uncarded_review_page", new=AsyncMock(return_value=self.result())) as query:
            await bot.show_uncarded_reviews.callback(ctx, 0)
            query.assert_not_awaited()
            await bot.show_uncarded_reviews.callback(ctx, 5)
        ctx.author.send.assert_not_awaited()
        self.assertIn("1~4", ctx.send.await_args.args[0])

    async def test_no_message_id_and_empty_queue_are_explicit(self):
        ctx = self.context()
        with patch.object(database, "get_uncarded_review_page", new=AsyncMock(return_value=self.result(1, None))):
            await bot.show_uncarded_reviews.callback(ctx)
        self.assertIn("메시지 ID 없음", ctx.author.send.await_args.kwargs["embed"].fields[0].value)
        ctx.author.send.reset_mock()
        with patch.object(database, "get_uncarded_review_page", new=AsyncMock(return_value=self.result(0))):
            await bot.show_uncarded_reviews.callback(ctx)
        ctx.author.send.assert_not_awaited()

    async def test_command_requires_guild_and_administrator(self):
        checks = bot.show_uncarded_reviews.checks
        self.assertEqual(len(checks), 2)
        with self.assertRaises(bot.commands.NoPrivateMessage):
            for check in checks:
                await bot.discord.utils.maybe_coroutine(
                    check, SimpleNamespace(guild=None, permissions=bot.discord.Permissions(administrator=True)))
        with self.assertRaises(bot.commands.MissingPermissions):
            for check in checks:
                await bot.discord.utils.maybe_coroutine(
                    check, SimpleNamespace(guild=SimpleNamespace(id=1), permissions=bot.discord.Permissions.none()))

    async def test_pending_summary_uses_true_total_and_all_age_scope(self):
        ctx = self.context()
        with (
            patch.object(database, "get_pending_reviews", new=AsyncMock(return_value=[])),
            patch.object(database, "get_stale_processing_reviews", new=AsyncMock(return_value=[])),
            patch.object(database, "get_uncarded_review_page", new=AsyncMock(return_value=self.result())),
        ):
            await bot.show_pending_reviews.callback(ctx)
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertIn("총 17건", embed.fields[0].name)
        self.assertIn("5건", embed.fields[0].name)
        self.assertIn("전체 기간", embed.description)
        self.assertIn("!BB 미전송검수", embed.fields[0].value)

    async def test_pending_summary_stays_within_embed_limits_with_long_legacy_text(self):
        ctx = self.context()
        long_text = "x" * 5000
        with (
            patch.object(database, "get_pending_reviews", new=AsyncMock(
                return_value=[(20, long_text, long_text, long_text, long_text, 100)] * 20)),
            patch.object(database, "get_stale_processing_reviews", new=AsyncMock(
                return_value=[(1, 20, 30, "MODERATE", long_text, 100)] * 10)),
            patch.object(database, "get_uncarded_review_page", new=AsyncMock(return_value=self.result())),
        ):
            await bot.show_pending_reviews.callback(ctx)
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertLessEqual(len(embed), 6000)
        self.assertTrue(all(len(f.name) <= 256 and len(f.value) <= 1024 for f in embed.fields))


if __name__ == "__main__":
    unittest.main()
