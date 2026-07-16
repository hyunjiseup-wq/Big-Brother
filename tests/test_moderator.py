import unittest
from unittest.mock import AsyncMock, patch

import moderator


class ModeratorBatchTests(unittest.IsolatedAsyncioTestCase):
    def test_batch_parser_rejects_non_object_items(self):
        with self.assertRaises(ValueError):
            moderator._parse_batch_json('[{"index": 0}, "bad"]')

    async def test_incomplete_batch_response_is_a_failure(self):
        messages = [
            {"index": 0, "author_ref": "user_1", "content": "a"},
            {"index": 1, "author_ref": "user_2", "content": "b"},
        ]
        response = [{"index": 0, "level": "NONE", "rule_violated": "NONE", "reason": ""}]
        with patch.object(
            moderator, "_classify_batch_with_gemini", new=AsyncMock(return_value=response)
        ):
            with self.assertRaises(moderator.BatchClassificationError):
                await moderator.classify_batch(messages, backend="gemini")


if __name__ == "__main__":
    unittest.main()
