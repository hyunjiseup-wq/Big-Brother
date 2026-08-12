import datetime
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import batch_audit
from moderator import BatchClassificationError, ModerationResult


def _message(message_id: int):
    return SimpleNamespace(
        id=message_id,
        content=f"message-{message_id}",
        author=SimpleNamespace(id=100 + message_id),
        created_at=datetime.datetime.now(datetime.UTC),
    )


class BatchAuditCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_failure_does_not_advance_checkpoint(self):
        channel = SimpleNamespace(id=10, name="test", guild=SimpleNamespace(id=1))
        messages = [_message(1), _message(2)]
        with (
            patch.object(batch_audit.database, "get_checkpoint", new=AsyncMock(return_value=None)),
            patch.object(batch_audit, "collect_messages", new=AsyncMock(return_value=messages)),
            patch.object(batch_audit, "get_channel_note", return_value=None),
            patch.object(batch_audit.learning, "get_prompt_examples", new=AsyncMock(return_value=None)),
            patch.object(batch_audit.learning, "is_known_false_positive", new=AsyncMock(return_value=False)),
            patch.object(
                batch_audit,
                "classify_batch",
                new=AsyncMock(side_effect=BatchClassificationError("provider unavailable")),
            ),
            patch.object(batch_audit.database, "set_checkpoint", new=AsyncMock()) as checkpoint,
        ):
            result = await batch_audit.audit_channel(channel, "gemini")

        self.assertEqual(result["reviewed_count"], 0)
        self.assertIsNotNone(result["error"])
        checkpoint.assert_not_awaited()

    async def test_partial_success_advances_only_to_last_successful_batch(self):
        channel = SimpleNamespace(id=10, name="test", guild=SimpleNamespace(id=1))
        messages = [_message(1), _message(2), _message(3)]
        first_results = [
            ModerationResult("NONE", "NONE", "", "gemini"),
            ModerationResult("NONE", "NONE", "", "gemini"),
        ]
        classifier = AsyncMock(side_effect=[first_results, BatchClassificationError("second failed")])
        with (
            patch.object(batch_audit.config, "BATCH_SIZE", 2),
            patch.object(batch_audit.database, "get_checkpoint", new=AsyncMock(return_value=None)),
            patch.object(batch_audit, "collect_messages", new=AsyncMock(return_value=messages)),
            patch.object(batch_audit, "get_channel_note", return_value=None),
            patch.object(batch_audit.learning, "get_prompt_examples", new=AsyncMock(return_value=None)),
            patch.object(batch_audit.learning, "is_known_false_positive", new=AsyncMock(return_value=False)),
            patch.object(batch_audit, "classify_batch", new=classifier),
            patch.object(batch_audit.database, "set_checkpoint", new=AsyncMock()) as checkpoint,
        ):
            result = await batch_audit.audit_channel(channel, "gemini")

        self.assertEqual(result["reviewed_count"], 2)
        self.assertIsNotNone(result["error"])
        checkpoint.assert_awaited_once_with(1, 10, 2)

    async def test_known_false_positive_skips_ai_but_advances_checkpoint(self):
        channel = SimpleNamespace(id=10, name="test", guild=SimpleNamespace(id=1))
        messages = [_message(1), _message(2)]
        with (
            patch.object(batch_audit.database, "get_checkpoint", new=AsyncMock(return_value=None)),
            patch.object(batch_audit, "collect_messages", new=AsyncMock(return_value=messages)),
            patch.object(batch_audit, "get_channel_note", return_value=None),
            patch.object(batch_audit.learning, "get_prompt_examples", new=AsyncMock(return_value=None)),
            patch.object(batch_audit.learning, "is_known_false_positive", new=AsyncMock(return_value=True)),
            patch.object(batch_audit, "classify_batch", new=AsyncMock()) as classifier,
            patch.object(batch_audit.database, "set_checkpoint", new=AsyncMock()) as checkpoint,
        ):
            result = await batch_audit.audit_channel(channel, "gemini")

        classifier.assert_not_awaited()
        checkpoint.assert_awaited_once_with(1, 10, 2)
        self.assertEqual(result["reviewed_count"], 2)

    async def test_barter_context_crosses_batch_and_checkpoint_boundaries(self):
        messages = [_message(1), _message(2)]

        async def history(*, before, **kwargs):
            if before.id == 2:
                yield messages[0]

        channel = SimpleNamespace(
            id=10, name="물물교환", guild=SimpleNamespace(id=1),
            parent=None, history=history,
        )
        classifier = AsyncMock(side_effect=[
            [ModerationResult("NONE", "NONE", "", "ollama")],
            [ModerationResult("NONE", "NONE", "", "ollama")],
        ])
        with (
            patch.object(batch_audit.config, "BARTER_CHANNEL_IDS", [10]),
            patch.object(batch_audit.config, "BATCH_SIZE", 1),
            patch.object(batch_audit.database, "get_checkpoint", new=AsyncMock(return_value=None)),
            patch.object(batch_audit, "collect_messages", new=AsyncMock(return_value=messages)),
            patch.object(batch_audit, "get_channel_note", return_value="물물교환 특수 규칙"),
            patch.object(batch_audit.learning, "get_prompt_examples", new=AsyncMock(return_value=None)),
            patch.object(batch_audit.learning, "is_known_false_positive", new=AsyncMock(return_value=False)),
            patch.object(batch_audit, "classify_batch", new=classifier),
            patch.object(batch_audit.database, "set_checkpoint", new=AsyncMock()),
        ):
            result = await batch_audit.audit_channel(channel, "ollama")

        self.assertEqual(result["reviewed_count"], 2)
        second_kwargs = classifier.await_args_list[1].kwargs
        self.assertTrue(second_kwargs["barter_context"])
        self.assertEqual(
            second_kwargs["conversation_context"][0]["content"], messages[0].content
        )


class BatchAuditTargetExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def test_expands_forum_into_active_and_recent_archived_threads(self):
        active = SimpleNamespace(id=11, name="active", history=object())
        recent = SimpleNamespace(
            id=12,
            name="recent",
            history=object(),
            archive_timestamp=datetime.datetime.now(datetime.UTC),
        )
        old = SimpleNamespace(
            id=13,
            name="old",
            history=object(),
            archive_timestamp=datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30),
        )

        async def archived_threads(*, limit):
            self.assertIsNone(limit)
            yield recent
            yield old

        forum = SimpleNamespace(
            id=10,
            name="물물교환",
            threads=[active],
            archived_threads=archived_threads,
        )
        guild = SimpleNamespace(get_channel=lambda channel_id: forum if channel_id == 10 else None)
        with (
            patch.object(batch_audit.config, "WATCHED_CHANNEL_IDS", [10]),
            patch.object(batch_audit.config, "BATCH_FIRST_RUN_LOOKBACK_DAYS", 7),
        ):
            targets = await batch_audit._expand_audit_targets(guild)

        self.assertEqual([target.id for target in targets], [11, 12])

    async def test_keeps_normal_text_channel_and_deduplicates_targets(self):
        channel = SimpleNamespace(id=20, name="자유", history=object())
        guild = SimpleNamespace(get_channel=lambda channel_id: channel)
        with patch.object(batch_audit.config, "WATCHED_CHANNEL_IDS", [20, 20]):
            targets = await batch_audit._expand_audit_targets(guild)

        self.assertEqual(targets, [channel])


class ReportRetentionTests(unittest.TestCase):
    def test_prunes_only_old_matching_report_files(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            batch_audit.config, "REPORT_OUTPUT_DIR", temp_dir
        ):
            old_report = os.path.join(temp_dir, "audit_report_20200101_000000.md")
            new_report = os.path.join(temp_dir, "audit_report_20990101_000000.md")
            unrelated = os.path.join(temp_dir, "notes.md")
            for path in (old_report, new_report, unrelated):
                with open(path, "w", encoding="utf-8") as file:
                    file.write("test")
            old_time = datetime.datetime.now().timestamp() - 100 * 86400
            os.utime(old_report, (old_time, old_time))

            self.assertEqual(batch_audit.prune_expired_reports(90), 1)
            self.assertFalse(os.path.exists(old_report))
            self.assertTrue(os.path.exists(new_report))
            self.assertTrue(os.path.exists(unrelated))

    def test_zero_retention_disables_report_pruning(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            batch_audit.config, "REPORT_OUTPUT_DIR", temp_dir
        ):
            report = os.path.join(temp_dir, "audit_report_20200101_000000.md")
            with open(report, "w", encoding="utf-8") as file:
                file.write("test")
            self.assertEqual(batch_audit.prune_expired_reports(0), 0)
            self.assertTrue(os.path.exists(report))


if __name__ == "__main__":
    unittest.main()
