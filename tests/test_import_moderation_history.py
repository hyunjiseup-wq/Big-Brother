import unittest

import import_moderation_history as importer


class ModerationHistoryImportTests(unittest.TestCase):
    def test_prepare_records_preserves_note_without_turning_it_into_warning(self):
        payload = {
            "schemaVersion": 1,
            "memberProfiles": {
                "1:50": {"tag": "대상유저"},
                "1:99": {"tag": "운영진"},
            },
            "sanctions": [{
                "id": "legacy-1", "guildId": "1", "type": "note",
                "targetUserId": "50", "targetTag": "old-target",
                "moderatorId": "99", "reason": "운영 참고 메모",
                "createdAt": "2026-04-08T01:27:41.700Z",
                "expiresAt": "2026-05-08T01:27:41.700Z",
                "releasedAt": None, "releaseReason": None, "active": False,
            }],
        }
        records, counts = importer.prepare_records(payload, 1)
        self.assertEqual(counts["NOTE"], 1)
        self.assertEqual(records[0]["action_type"], "NOTE")
        self.assertEqual(records[0]["status"], "expired")
        self.assertEqual(records[0]["issued_by_id"], 99)
        self.assertEqual(records[0]["issued_by_display"], "운영진")
        self.assertEqual(records[0]["user_display"], "대상유저")

    def test_prepare_records_excludes_other_guild(self):
        payload = {
            "schemaVersion": 1,
            "memberProfiles": {},
            "sanctions": [{
                "id": "other-1", "guildId": "2", "type": "warn",
                "targetUserId": "50", "moderatorId": "99", "reason": "다른 서버",
                "createdAt": "2026-04-08T01:27:41.700Z", "active": True,
            }],
        }
        records, counts = importer.prepare_records(payload, 1)
        self.assertEqual(records, [])
        self.assertEqual(counts["excluded_other_guild"], 1)

    def test_unsupported_type_is_rejected(self):
        payload = {
            "schemaVersion": 1,
            "memberProfiles": {},
            "sanctions": [{
                "id": "legacy-1", "guildId": "1", "type": "unknown",
                "targetUserId": "50", "moderatorId": "99", "reason": "x",
                "createdAt": "2026-04-08T01:27:41.700Z", "active": True,
            }],
        }
        with self.assertRaisesRegex(ValueError, "지원하지 않는 제재 유형"):
            importer.prepare_records(payload, 1)
