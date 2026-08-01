import unittest
from unittest.mock import patch

import config


class ConfigValidationTests(unittest.TestCase):
    def test_channel_id_environment_list_parser(self):
        self.assertEqual(config._parse_channel_id_list("123, 456"), [123, 456])
        self.assertEqual(config._parse_channel_id_list(""), [])

    def test_invalid_channel_id_token_is_preserved_for_validation(self):
        self.assertEqual(config._parse_channel_id_list("123,broken"), [123, "broken"])

    def test_current_configuration_is_valid(self):
        config.validate_config()

    def test_invalid_batch_backend_is_rejected(self):
        with patch.object(config, "BATCH_BACKEND", "unknown"):
            with self.assertRaisesRegex(ValueError, "BATCH_BACKEND"):
                config.validate_config()

    def test_invalid_watched_channel_id_is_rejected(self):
        with patch.object(config, "WATCHED_CHANNEL_IDS", [123, "not-an-id"]):
            with self.assertRaisesRegex(ValueError, "WATCHED_CHANNEL_IDS"):
                config.validate_config()

    def test_timeout_without_positive_duration_is_rejected(self):
        with patch.object(config, "STRIKE_THRESHOLDS", [(1, "TIMEOUT", None)]):
            with self.assertRaisesRegex(ValueError, "TIMEOUT"):
                config.validate_config()

    def test_malformed_threshold_row_reports_configuration_error(self):
        with patch.object(config, "STRIKE_THRESHOLDS", [(1, "WARN")]):
            with self.assertRaisesRegex(ValueError, "STRIKE_THRESHOLDS"):
                config.validate_config()

    def test_negative_content_retention_is_rejected(self):
        with patch.object(config, "VIOLATION_CONTENT_RETENTION_DAYS", -1):
            with self.assertRaisesRegex(ValueError, "VIOLATION_CONTENT_RETENTION_DAYS"):
                config.validate_config()

    def test_negative_report_retention_is_rejected(self):
        with patch.object(config, "REPORT_RETENTION_DAYS", -1):
            with self.assertRaisesRegex(ValueError, "REPORT_RETENTION_DAYS"):
                config.validate_config()
