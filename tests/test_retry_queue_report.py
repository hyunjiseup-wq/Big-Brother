import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from retry_queue_report import inspect_queue


class RetryQueueReportTests(unittest.TestCase):
    def test_age_or_forbidden_alone_are_not_cleanup_candidates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "queue.db"
            now = 40 * 86400
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE moderation_retry_queue "
                           "(guild_id, created_at, attempts, last_failure)")
                db.executemany("INSERT INTO moderation_retry_queue VALUES (?,?,?,?)", [
                    (1, 0, 3, "discord_guild_unavailable"),
                    (1, 0, 2, "discord_guild_unavailable"),
                    (2, now - 29 * 86400, 99, "discord_guild_unavailable"),
                    (2, 0, 99, "discord_channel_unavailable"),
                    (3, now - 30 * 86400, 3, "discord_guild_unavailable"),
                ])
                db.commit()
            before = path.read_bytes()
            result = inspect_queue(path, now=now)
            self.assertEqual(result["pending"], 5)
            self.assertEqual([g["investigation_candidates"] for g in result["guilds"].values()], [1, 0, 1])
            self.assertFalse(result["deletion_authorized"])
            self.assertEqual(path.read_bytes(), before)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing.db"
            with self.assertRaises(sqlite3.OperationalError):
                inspect_queue(path)
            self.assertFalse(path.exists())
