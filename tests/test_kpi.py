import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_audit
import database
import kpi


class KpiPeriodTests(unittest.TestCase):
    def test_completed_calendar_periods_use_korean_time(self):
        now = datetime.datetime(2026, 8, 12, 15, 0, tzinfo=kpi.KST)
        month = kpi.completed_period("month", now)
        quarter = kpi.completed_period("quarter", now)
        year = kpi.completed_period("year", now)

        self.assertEqual((month.key, month.start.day, month.end.month), ("2026-07", 1, 8))
        self.assertEqual((quarter.key, quarter.start.month, quarter.end.month), ("2026-Q2", 4, 7))
        self.assertEqual((year.key, year.start.year, year.end.year), ("2025", 2025, 2026))
        self.assertEqual(month.start.utcoffset(), datetime.timedelta(hours=9))

    def test_context_categories_are_stable_and_public_safe(self):
        self.assertEqual(kpi.categorize_detection("3", "개인 DM 현금 거래 유도"), "rmt_barter")
        self.assertEqual(kpi.categorize_detection("2", "다른 서버 초대 링크"), "ads_links_invites")
        self.assertEqual(kpi.categorize_detection("3", "반말과 욕설"), "language_etiquette")
        self.assertEqual(kpi.categorize_detection("4", "상대 조롱과 도발"), "conflict_mockery")

    def test_staff_sanction_event_keeps_identity_but_hides_guild_id(self):
        row = {
            "sanction_id": 7, "guild_id": 123456, "user_id": 50,
            "user_display": "테스트유저", "action_type": "WARNING",
            "reason": "운영진 수동 경고", "source": "manual_command",
            "status": "active", "issued_at": 100, "expires_at": None,
            "released_at": None, "issued_by_id": 99,
            "issued_by_display": "관리자", "released_by_id": None,
            "released_by_display": None, "release_reason": None,
        }
        event = kpi.build_sync_sanction(row)
        self.assertEqual(event["user_id"], "50")
        self.assertEqual(event["reason"], "운영진 수동 경고")
        self.assertNotIn("123456", event["event_id"])


class KpiDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        await database.init_db()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def _review(self, message_id, level, reason, rule, provider, source):
        return await database.create_review_record(
            1, 50, 10, message_id, f"private-content-{message_id}", level, reason,
            "검수 대기", provider, rule_violated=rule, detection_source=source,
        )

    async def test_review_changes_are_durably_queued_without_private_content(self):
        review_id = await self._review(
            1, "MODERATE", "현금 거래 의심", "3", "ollama", "batch"
        )
        rows = await database.get_due_kpi_sync_records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["review_id"], review_id)
        self.assertEqual(rows[0]["detection_source"], "batch")
        self.assertEqual(rows[0]["rule_violated"], "3")
        self.assertNotIn("message_content", rows[0])
        self.assertNotIn("user_id", rows[0])
        self.assertNotIn("message_id", rows[0])

        event = kpi.build_sync_event(rows[0], "테스트", "테스트 그룹")
        serialized = str(event)
        self.assertNotIn("private-content", serialized)
        self.assertNotIn("message_content", event)
        self.assertNotIn("user_id", event)
        self.assertNotIn("message_id", event)
        self.assertNotEqual(
            str(rows[0]["guild_id"]), event["event_id"].split(":", 1)[0]
        )

        await database.mark_kpi_sync_complete([review_id])
        self.assertEqual(await database.count_kpi_sync_pending(), 0)
        self.assertTrue(await database.claim_review(review_id, 1))
        self.assertTrue(await database.resolve_review(
            review_id, 1, "confirmed", 99, "메시지 삭제"
        ))
        self.assertEqual(await database.count_kpi_sync_pending(), 1)

    async def test_summary_calculates_precision_channels_and_review_time(self):
        false_positive = await self._review(
            1, "MINOR", "반말 말투", "3", "groq", "realtime"
        )
        confirmed = await self._review(
            2, "MODERATE", "외부 서버 초대 링크", "2", "ollama", "batch"
        )
        self.assertTrue(await database.claim_review(false_positive, 1))
        result = await database.resolve_review_as_false_positive(
            false_positive, 1, 10, "hash", "safe", 99, "정상 처리"
        )
        self.assertIsNotNone(result)
        self.assertTrue(await database.claim_review(confirmed, 1))
        self.assertTrue(await database.resolve_review(
            confirmed, 1, "confirmed", 99, "메시지 삭제"
        ))

        now = datetime.datetime.now(kpi.KST)
        period = kpi.KpiPeriod(
            "month", "test", "테스트 기간",
            now - datetime.timedelta(days=1), now + datetime.timedelta(days=1),
        )
        summary = await kpi.build_summary(1, period, {10: "#테스트"})
        self.assertEqual(summary["cards"]["detected"], 2)
        self.assertEqual(summary["cards"]["confirmed"], 1)
        self.assertEqual(summary["cards"]["false_positive"], 1)
        self.assertEqual(summary["cards"]["precision_percent"], 50.0)
        self.assertEqual(summary["top"]["false_positive_channels"][0]["label"], "#테스트")
        self.assertEqual(summary["sources"], {"realtime": 1, "batch": 1})

    async def test_report_delivery_key_is_idempotent(self):
        self.assertFalse(await database.kpi_report_was_delivered(1, "month", "2026-07"))
        await database.mark_kpi_report_delivered(1, "month", "2026-07", 10, 20)
        await database.mark_kpi_report_delivered(1, "month", "2026-07", 11, 21)
        self.assertTrue(await database.kpi_report_was_delivered(1, "month", "2026-07"))

    async def test_completed_period_snapshot_is_reused_then_invalidated_by_review(self):
        review_id = await self._review(
            1, "MINOR", "반말 말투", "3", "groq", "realtime"
        )
        current = datetime.datetime.now(kpi.KST)
        period = kpi.KpiPeriod(
            "month", "snapshot-test", "스냅샷 테스트",
            current - datetime.timedelta(hours=1),
            current + datetime.timedelta(hours=1),
        )
        future = current + datetime.timedelta(hours=2)
        first = await kpi.get_period_summary(1, period, now=future)
        self.assertEqual(first["cards"]["pending"], 1)
        snapshot = await database.get_kpi_period_snapshot(1, "month", "snapshot-test")
        self.assertFalse(snapshot["dirty"])
        self.assertEqual(json.loads(snapshot["summary_json"])["cards"]["pending"], 1)

        self.assertTrue(await database.claim_review(review_id, 1))
        self.assertTrue(await database.resolve_review(
            review_id, 1, "confirmed", 99, "관리자 확정"
        ))
        snapshot = await database.get_kpi_period_snapshot(1, "month", "snapshot-test")
        self.assertTrue(snapshot["dirty"])
        refreshed = await kpi.get_period_summary(1, period, now=future)
        self.assertEqual(refreshed["cards"]["confirmed"], 1)
        self.assertFalse((await database.get_kpi_period_snapshot(
            1, "month", "snapshot-test"
        ))["dirty"])

    async def test_audit_metrics_and_legacy_report_backfill_are_idempotent(self):
        with tempfile.TemporaryDirectory() as report_dir:
            path = Path(report_dir) / "audit_report_20260801_090000.md"
            path.write_text(
                "# 채팅 감사 리포트\n\n"
                "- 검토한 메시지: 100건\n"
                "- 규정 위반 의심: 8건\n"
                "- ⚠️ 판단 실패로 재시도가 필요한 채널: 1개\n\n"
                "### 채널별 검토 범위\n- #자유: 50건\n- #PVE: 50건\n",
                encoding="utf-8",
            )
            with patch.object(batch_audit.config, "REPORT_OUTPUT_DIR", report_dir):
                self.assertEqual(await batch_audit.backfill_audit_metrics_from_reports(1), 1)
                self.assertEqual(await batch_audit.backfill_audit_metrics_from_reports(1), 0)

            queued = await database.get_due_audit_kpi_sync_records()
            self.assertEqual(len(queued), 1)
            self.assertNotIn("report_path", queued[0])
            audit_event = kpi.build_sync_audit_event(queued[0])
            self.assertEqual(audit_event["reviewed_messages"], 100)
            self.assertEqual(audit_event["flag_rate_percent"], 8.0)
            self.assertNotEqual(
                str(queued[0]["guild_id"]), audit_event["event_id"].split(":", 1)[0]
            )

            metrics = await database.get_audit_metrics(1, 0, float("inf"))
            self.assertEqual(metrics["runs"], 1)
            self.assertEqual(metrics["reviewed_messages"], 100)
            self.assertEqual(metrics["flagged_messages"], 8)
            self.assertEqual(metrics["flag_rate_percent"], 8.0)
            self.assertEqual(metrics["failed_channels"], 1)
            self.assertEqual(metrics["successful_channel_percent"], 50.0)

            operations = kpi.build_sync_operations(1, {
                "pending_over_24h": 2,
                "pending_over_72h": 1,
                "active_learning_rules": 12,
                "ai_retry_queue": 3,
                "kpi_sync_pending": 4,
            })
            self.assertNotEqual(operations["scope"], "1")
            self.assertEqual(operations["ai_retry_queue"], 3)


if __name__ == "__main__":
    unittest.main()
