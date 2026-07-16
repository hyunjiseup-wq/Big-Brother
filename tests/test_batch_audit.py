import datetime
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


if __name__ == "__main__":
    unittest.main()
