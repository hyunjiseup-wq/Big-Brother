import json
import tempfile
import unittest
from pathlib import Path

from policy_loader import load_policy_file


class PolicyLoaderTests(unittest.TestCase):
    def test_loads_rules_and_normalizes_numeric_channel_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({
                "server_rules": "  community rules  ",
                "channel_context_notes": {
                    "123": " report channel ",
                    "trade-room": "trade context",
                },
            }), encoding="utf-8")
            rules, notes = load_policy_file(path)
        self.assertEqual(rules, "community rules")
        self.assertEqual(notes, {123: "report channel", "trade-room": "trade context"})

    def test_rejects_missing_or_malformed_policy(self):
        with self.assertRaisesRegex(ValueError, "찾을 수 없습니다"):
            load_policy_file("definitely-missing-policy.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text('{"server_rules": ""}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "server_rules"):
                load_policy_file(path)

    def test_rejects_invalid_channel_note(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({
                "server_rules": "rules",
                "channel_context_notes": {"general": 123},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "특수 규칙"):
                load_policy_file(path)
