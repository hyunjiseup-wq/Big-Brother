import asyncio
import os
import tempfile
import time
import unittest

import aiosqlite

import database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        await database.init_db()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_decay_is_not_reapplied_on_every_read(self):
        old = time.time() - 31 * 86400
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO strikes VALUES (?, ?, ?, ?, ?)",
                (1, 2, 8, old, old),
            )
            await db.commit()

        values = [await database.get_points(1, 2) for _ in range(3)]
        self.assertEqual(values, [4.0, 4.0, 4.0])

    async def test_concurrent_point_updates_are_not_lost(self):
        await asyncio.gather(*(database.add_points(1, 3, 1) for _ in range(20)))
        self.assertEqual(await database.get_points(1, 3), 20)

    async def test_decay_read_cannot_overwrite_concurrent_add(self):
        old = time.time() - 31 * 86400
        async with aiosqlite.connect(database.DB_PATH) as db:
            await db.execute(
                "INSERT INTO strikes VALUES (?, ?, ?, ?, ?)",
                (1, 4, 8, old, old),
            )
            await db.commit()
        await asyncio.gather(
            database.get_points(1, 4),
            database.add_points(1, 4, 1),
        )
        self.assertEqual(await database.get_points(1, 4), 5)

    async def test_review_can_only_be_claimed_once_and_false_positive_is_hidden(self):
        review_id = await database.log_violation(
            1, 9, 10, "테스트", "MINOR", "테스트", "검토 대기",
            review_status="pending", message_id=11,
        )
        first, second = await asyncio.gather(
            database.claim_review(review_id, 1),
            database.claim_review(review_id, 1),
        )
        self.assertEqual(sum((first, second)), 1)
        await database.resolve_review(review_id, 1, "false_positive", 99, "정상 처리")
        self.assertEqual(await database.get_recent_violations(1, 9), [])

    async def test_false_positive_review_and_rule_are_saved_atomically(self):
        review_id = await database.log_violation(
            1, 9, 10, "safe message", "MODERATE", "wrong", "검수 대기",
            review_status="pending", message_id=12,
        )
        self.assertTrue(await database.claim_review(review_id, 1))
        result = await database.resolve_review_as_false_positive(
            review_id, 1, 10, "hash", "safe message", 99, "정상 처리",
        )
        self.assertIsNotNone(result)
        rules = await database.list_false_positive_rules(1)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0][1:3], (10, "safe message"))
        self.assertFalse(await database.release_review(review_id, 1))

    async def test_processing_review_requires_explicit_recovery(self):
        review_id = await database.create_review_record(
            1, 50, 10, 999, "message", "MODERATE", "reason", "검수 대기"
        )
        self.assertTrue(await database.claim_review(review_id, 1))

        # DB 초기화(봇 재시작)가 처리 중 건을 자동 재시도 상태로 바꾸면 안 된다.
        await database.init_db()
        self.assertFalse(await database.claim_review(review_id, 1))

        stalled = await database.get_stale_processing_reviews(1, minutes=1)
        self.assertEqual(stalled, [])  # 방금 선점한 건은 아직 중단 의심 대상이 아니다.
        self.assertTrue(await database.recover_processing_review(review_id, 1))
        self.assertTrue(await database.claim_review(review_id, 1))

    async def test_retention_redacts_only_old_completed_content(self):
        old = time.time() - 100 * 86400
        async with aiosqlite.connect(database.DB_PATH) as db:
            for message_id, status, created_at in (
                (101, "not_required", old),
                (102, "pending", old),
                (103, "not_required", time.time()),
            ):
                await db.execute(
                    """INSERT INTO violation_log
                       (guild_id, user_id, channel_id, message_id, message_content, level,
                        review_status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (1, 2, 3, message_id, f"content-{message_id}", "MINOR", status, created_at),
                )
            await db.commit()

        self.assertEqual(await database.redact_expired_violation_content(90), 1)
        async with aiosqlite.connect(database.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT message_id, message_content FROM violation_log ORDER BY message_id"
            )
            rows = await cursor.fetchall()
        self.assertEqual(rows, [(101, None), (102, "content-102"), (103, "content-103")])

    async def test_zero_retention_disables_redaction(self):
        self.assertEqual(await database.redact_expired_violation_content(0), 0)


if __name__ == "__main__":
    unittest.main()
