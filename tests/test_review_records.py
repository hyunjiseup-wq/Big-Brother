"""검수 레코드 생성·카드 전송 실패·배치 카드 상한 초과분 보존에 대한 회귀 테스트."""
import datetime
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite

import batch_audit
import database


def _message(message_id: int, guild_id: int = 1, channel_id: int = 10):
    return SimpleNamespace(
        id=message_id,
        content=f"message-{message_id}",
        author=SimpleNamespace(id=100 + message_id),
        channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=guild_id),
        created_at=datetime.datetime.now(datetime.UTC),
    )


class ReviewRecordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        await database.init_db()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def _status(self, review_id):
        async with aiosqlite.connect(database.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT review_status, card_delivered FROM violation_log WHERE id = ?",
                (review_id,),
            )
            return await cursor.fetchone()

    async def test_recheck_of_edited_message_supersedes_instead_of_crashing(self):
        """같은 메시지가 수정돼 다시 검사돼도 유니크 인덱스 충돌 없이 새 검수가 생성된다."""
        first = await database.create_review_record(
            1, 50, 10, 999, "before edit", "MINOR", "reason", "검수대기", "gemini")
        second = await database.create_review_record(
            1, 50, 10, 999, "after edit", "MODERATE", "reason", "검수대기", "gemini")

        self.assertNotEqual(first, second)
        self.assertEqual((await self._status(first))[0], "superseded")
        self.assertEqual((await self._status(second))[0], "pending")
        # 밀려난 예전 카드의 버튼은 더 이상 동작하지 않아야 한다 (이중 처리 방지).
        self.assertFalse(await database.claim_review(first, 1))
        self.assertTrue(await database.claim_review(second, 1))

    async def test_delivery_failure_is_recorded_and_queryable(self):
        review_id = await database.create_review_record(
            1, 50, 10, 111, "content", "MODERATE", "reason", "검수대기", "gemini")
        self.assertEqual(await database.get_reviews_without_card(1), [])

        await database.mark_review_delivery_failed(review_id, 1)

        self.assertEqual((await self._status(review_id))[1], 0)
        rows = await database.get_reviews_without_card(1)
        self.assertEqual([row[0] for row in rows], [review_id])

    async def test_flagged_beyond_card_limit_is_still_persisted(self):
        """카드 상한을 넘은 건도 DB에 남아야 리포트 밖에서 영영 누락되지 않는다."""
        flagged = [
            {"message": _message(i), "level": "MODERATE", "rule_violated": "3",
             "reason": f"reason-{i}", "provider": "gemini"}
            for i in range(1, 6)
        ]
        audit_results = [{"flagged": flagged}]
        on_flagged = AsyncMock()

        original_limit = batch_audit.config.BATCH_REVIEW_CARD_LIMIT
        batch_audit.config.BATCH_REVIEW_CARD_LIMIT = 2
        try:
            await batch_audit._post_review_cards(audit_results, on_flagged)
        finally:
            batch_audit.config.BATCH_REVIEW_CARD_LIMIT = original_limit

        # 카드는 상한만큼만 게시되지만, 검수 레코드는 5건 전부 저장된다.
        self.assertEqual(on_flagged.await_count, 2)
        async with aiosqlite.connect(database.DB_PATH) as db:
            cursor = await db.execute(
                "SELECT card_delivered, COUNT(*) FROM violation_log "
                "WHERE review_status = 'pending' GROUP BY card_delivered ORDER BY card_delivered")
            counts = dict(await cursor.fetchall())
        self.assertEqual(counts, {0: 3, 1: 2})
        # 카드 없는 3건은 검토대기 명령어로 확인할 수 있어야 한다.
        self.assertEqual(len(await database.get_reviews_without_card(1)), 3)

    async def test_card_send_failure_during_batch_marks_record(self):
        flagged = [{"message": _message(1), "level": "SEVERE", "rule_violated": "4",
                    "reason": "reason", "provider": "gemini"}]
        on_flagged = AsyncMock(side_effect=RuntimeError("send failed"))

        await batch_audit._post_review_cards([{"flagged": flagged}], on_flagged)

        rows = await database.get_reviews_without_card(1)
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
