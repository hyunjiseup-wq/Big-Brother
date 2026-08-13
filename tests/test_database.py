import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

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

    async def test_initialized_database_passes_integrity_check(self):
        await database.validate_database_integrity()

    async def test_corrupt_existing_database_is_rejected_before_initialization(self):
        corrupt_path = os.path.join(self.temp_dir.name, "corrupt.db")
        with open(corrupt_path, "wb") as file:
            file.write(b"not a sqlite database")
        database.DB_PATH = corrupt_path

        with self.assertRaises(aiosqlite.DatabaseError):
            await database.init_db()

    async def test_online_backup_is_complete_and_valid(self):
        await database.add_points(1, 77, 3)
        backup_dir = os.path.join(self.temp_dir.name, "backups")
        backup_path = await database.create_database_backup(backup_dir)

        self.assertTrue(backup_path.is_file())
        async with aiosqlite.connect(backup_path) as backup_db:
            points = await (await backup_db.execute(
                "SELECT points FROM strikes WHERE guild_id = 1 AND user_id = 77"
            )).fetchone()
            check = await (await backup_db.execute("PRAGMA quick_check")).fetchone()
        self.assertEqual(points, (3.0,))
        self.assertEqual(check, ("ok",))
        self.assertEqual(list(backup_path.parent.glob("*.partial")), [])

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
        examples = await database.get_moderation_training_examples(1)
        self.assertEqual(examples[0][:3], ("safe message", "normal", "NONE"))

    async def test_review_preserves_learning_scope_and_thread_rules_can_be_migrated(self):
        review_id = await database.create_review_record(
            1, 9, 20, 120, "safe trade", "MODERATE", "wrong", "검수 대기",
            learning_scope_channel_id=20,
        )
        self.assertEqual(await database.get_review_learning_scope(review_id, 1), 20)
        await database.upsert_false_positive_rule(
            1, 20, "trade-hash", "safe trade", "MODERATE", "wrong", 20, 99,
        )

        moved = await database.migrate_false_positive_thread_scope(1, 20, 20, 10)

        self.assertEqual(moved, 1)
        self.assertEqual(await database.get_review_learning_scope(review_id, 1), 10)
        rules = await database.list_false_positive_rules(1)
        self.assertEqual([(row[1], row[2]) for row in rules], [(10, "safe trade")])
        candidates = await database.get_false_positive_thread_scope_candidates()
        self.assertEqual(candidates, [])

    async def test_sanction_lifecycle_is_durable_and_queued_for_staff_dashboard(self):
        sanction_id = await database.record_sanction(
            1, 50, "테스트유저", "TIMEOUT", "분쟁 유발", "manual_command",
            "manual:1", issued_by_id=99, issued_by_display="관리자",
            issued_at=100, expires_at=200,
        )
        due = await database.get_due_sanction_sync_records()
        self.assertEqual([row["sanction_id"] for row in due], [sanction_id])
        self.assertEqual(due[0]["reason"], "분쟁 유발")
        self.assertEqual(await database.count_kpi_sync_pending(), 1)

        await database.mark_sanction_sync_complete([sanction_id])
        self.assertEqual(await database.count_kpi_sync_pending(), 0)
        released = await database.release_active_sanctions(
            1, 50, "TIMEOUT", "상황 종료", released_by_id=99,
            released_by_display="관리자", released_at=150,
        )
        self.assertEqual(released, [sanction_id])
        history = await database.get_sanction_history(1, 50)
        self.assertEqual(history[0]["status"], "released")
        self.assertEqual(history[0]["released_at"], 150)
        self.assertEqual(history[0]["release_reason"], "상황 종료")
        self.assertEqual(await database.count_kpi_sync_pending(), 1)

    async def test_elapsed_timeout_is_marked_expired(self):
        sanction_id = await database.record_sanction(
            1, 50, "테스트유저", "TIMEOUT", "테스트", "review", "review:1",
            issued_at=100, expires_at=200,
        )
        await database.mark_sanction_sync_complete([sanction_id])
        self.assertEqual(await database.expire_elapsed_timeouts(now=201), 1)
        history = await database.get_sanction_history(1, 50)
        self.assertEqual(history[0]["status"], "expired")
        self.assertEqual(history[0]["released_at"], 200)

    async def test_confirmed_review_creates_violation_training_label(self):
        review_id = await database.log_violation(
            1, 9, 10, "harmful message", "SEVERE", "confirmed reason", "review",
            review_status="pending", message_id=13,
        )
        self.assertTrue(await database.claim_review(review_id, 1))
        self.assertTrue(await database.resolve_review(
            review_id, 1, "confirmed", 99, "confirmed action",
        ))

        examples = await database.get_moderation_training_examples(1)
        self.assertEqual(examples[0][:3], ("harmful message", "violation", "SEVERE"))

    async def test_unconfirmed_review_is_not_a_training_example(self):
        await database.log_violation(
            1, 9, 10, "pending message", "MINOR", "model guess", "review",
            review_status="pending", message_id=14,
        )
        self.assertEqual(await database.get_moderation_training_examples(1), [])

    async def test_training_stats_are_grouped_by_language_and_verdict(self):
        for message_id, content, status in (
            (21, "정상적인 한국어", "false_positive"),
            (22, "harmful latin text", "confirmed"),
        ):
            review_id = await database.log_violation(
                1, 9, 10, content, "MODERATE", "reason", "review",
                review_status="pending", message_id=message_id,
            )
            self.assertTrue(await database.claim_review(review_id, 1))
            self.assertTrue(await database.resolve_review(review_id, 1, status, 99, "done"))

        self.assertEqual(
            set(await database.get_moderation_label_stats(1)),
            {("ko", "normal", 1), ("latin", "violation", 1)},
        )

    async def test_processing_review_requires_explicit_recovery(self):
        review_id = await database.create_review_record(
            1, 50, 10, 999, "message", "MODERATE", "reason", "검수 대기"
        )
        with patch.object(database.time, "time", return_value=1):
            self.assertTrue(await database.claim_review(review_id, 1))

        # DB 초기화(봇 재시작)가 처리 중 건을 자동 재시도 상태로 바꾸면 안 된다.
        await database.init_db()
        self.assertFalse(await database.claim_review(review_id, 1))

        stalled = await database.get_stale_processing_reviews(1, minutes=1)
        self.assertEqual([row[0] for row in stalled], [review_id])
        self.assertTrue(await database.recover_processing_review(review_id, 1))
        self.assertTrue(await database.claim_review(review_id, 1))

    async def test_active_processing_review_cannot_be_recovered(self):
        review_id = await database.create_review_record(
            1, 50, 10, 1000, "message", "MODERATE", "reason", "검수 대기"
        )
        self.assertTrue(await database.claim_review(review_id, 1))
        self.assertFalse(await database.recover_processing_review(review_id, 1))
        self.assertFalse(await database.claim_review(review_id, 1))

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

    async def test_ai_retry_queue_is_durable_deduplicated_and_reschedulable(self):
        with patch.object(database.time, "time", return_value=100):
            await database.enqueue_moderation_retry(1, 10, 99, "rate_limit", 30)
            await database.enqueue_moderation_retry(1, 10, 99, "rate_limit", 60)
        self.assertEqual(await database.count_moderation_retries(1), 1)

        await database.init_db()
        with patch.object(database.time, "time", return_value=131):
            rows = await database.get_due_moderation_retries()
        self.assertEqual(len(rows), 1)
        retry_id = rows[0][0]

        with patch.object(database.time, "time", return_value=131):
            await database.reschedule_moderation_retry(retry_id, 1, "timeout", 60)
        with patch.object(database.time, "time", return_value=190):
            self.assertEqual(await database.get_due_moderation_retries(), [])
        with patch.object(database.time, "time", return_value=192):
            self.assertEqual((await database.get_due_moderation_retries())[0][4], 1)

        await database.delete_moderation_retry_for_message(1, 99)
        self.assertEqual(await database.count_moderation_retries(), 0)


if __name__ == "__main__":
    unittest.main()
