import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import database
import learning


class LearningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        learning._reset_for_tests()
        await database.init_db()

    async def asyncTearDown(self):
        learning._reset_for_tests()
        self.temp_dir.cleanup()

    async def test_channel_rule_does_not_leak_to_other_channel(self):
        await learning.record_false_positive(1, 10, "Normal Text", "MINOR", "wrong", 99)
        self.assertTrue(await learning.is_known_false_positive(1, 10, " normal   text "))
        self.assertFalse(await learning.is_known_false_positive(1, 11, "Normal Text"))

    async def test_server_rule_applies_to_all_channels(self):
        await learning.record_false_positive(
            1, 10, "server safe", "MINOR", "wrong", 99, server_wide=True,
        )
        self.assertTrue(await learning.is_known_false_positive(1, 999, "server safe"))

    async def test_thread_uses_parent_channel_scope_and_nfkc(self):
        thread = SimpleNamespace(id=101, parent_id=10)
        await learning.record_false_positive(1, thread, "Ａ\u200bＢ normal", "MINOR", "wrong", 99)
        self.assertTrue(await learning.is_known_false_positive(1, 10, "ab normal"))

    async def test_short_or_keyword_false_positive_requires_context_again(self):
        await learning.record_false_positive(1, 10, "네", "MINOR", "wrong", 99)
        await learning.record_false_positive(1, 10, "시발", "MINOR", "wrong", 99)
        self.assertFalse(await learning.is_known_false_positive(1, 10, "네"))
        self.assertFalse(await learning.is_known_false_positive(1, 10, "시발"))
        self.assertIsNone(await learning.get_prompt_examples(1, 10))

    async def test_admin_explanation_makes_contextual_example_useful_without_auto_skip(self):
        await learning.record_false_positive(
            1, 10, "네", "MODERATE", "현금 거래 동조로 오판", 99,
            learning_explanation=(
                "답글 원문의 계좌 거래 질문을 부정하고 게임 내 플리마켓을 설명한 대화임"
            ),
        )
        self.assertFalse(await learning.is_known_false_positive(1, 10, "네"))
        examples = await learning.get_prompt_examples(1, 10)
        self.assertEqual(examples[0]["content"], "네")
        self.assertIn("플리마켓", examples[0]["administrator_explanation"])

    async def test_prompt_examples_are_sanitized_and_rule_can_be_removed(self):
        rule_id = await learning.record_false_positive(
            1, 10, "@everyone see https://example.com\x01", "MINOR", "wrong", 99,
        )
        examples = await learning.get_prompt_examples(1, 10)
        self.assertEqual(examples[0]["content"], "[MENTION] see [URL]")
        self.assertTrue(await learning.remove_rule(1, rule_id))
        self.assertFalse(await learning.is_known_false_positive(
            1, 10, "@everyone see https://example.com\x01"
        ))

    async def test_channel_examples_are_prioritized_over_newer_server_examples(self):
        await learning.record_false_positive(
            1, 10, "channel-specific safe trade", "MODERATE", "wrong", 99,
        )
        await learning.record_false_positive(
            1, 10, "newer server-wide example", "MODERATE", "wrong", 99,
            server_wide=True,
        )
        learning._examples_cache.clear()
        with patch.object(learning.config, "FALSE_POSITIVE_PROMPT_EXAMPLES", 1):
            examples = await learning.get_prompt_examples(1, 10)

        self.assertEqual(examples[0]["scope"], "channel")
        self.assertEqual(examples[0]["content"], "channel-specific safe trade")

    async def test_historical_false_positive_is_backfilled_once(self):
        review_id = await database.log_violation(
            1, 9, 10, "old safe", "MODERATE", "wrong", "pending",
            review_status="pending", message_id=12,
        )
        await database.claim_review(review_id, 1)
        await database.resolve_review(review_id, 1, "false_positive", 99, "normal")
        learning._reset_for_tests()
        self.assertEqual(await learning.initialize(), 1)
        self.assertTrue(await learning.is_known_false_positive(1, 10, "old safe"))
        learning._reset_for_tests()
        self.assertEqual(await learning.initialize(), 0)


if __name__ == "__main__":
    unittest.main()
