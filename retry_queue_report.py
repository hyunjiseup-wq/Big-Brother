"""Read-only cleanup candidates. Age/cache absence alone never authorizes deletion."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

MIN_AGE_DAYS = 30
MIN_TOTAL_ATTEMPTS = 3


def inspect_queue(path, *, now=None):
    now = time.time() if now is None else now
    # mode=ro prevents both schema changes and accidentally creating an empty database.
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute(
            "SELECT guild_id, created_at, attempts, last_failure FROM moderation_retry_queue"
        ).fetchall()
    groups = {}
    for guild_id, created_at, attempts, failure in rows:
        group = groups.setdefault(str(guild_id), {"pending": 0, "oldest_days": 0,
                                                 "investigation_candidates": 0})
        age = max(0, now - created_at) / 86400
        group["pending"] += 1
        group["oldest_days"] = max(group["oldest_days"], round(age, 2))
        if (age >= MIN_AGE_DAYS and attempts >= MIN_TOTAL_ATTEMPTS
                and failure == "discord_guild_unavailable"):
            group["investigation_candidates"] += 1
    return {"read_only": True, "pending": len(rows), "guilds": groups,
            "deletion_authorized": False,
            "criteria": "30d age + >=3 total attempts + last failure guild unavailable; "
                        "not proof of departure; confirm membership twice >=24h apart, "
                        "staff approval and verified backup required"}


if __name__ == "__main__":
    import database
    try:
        print(json.dumps(inspect_queue(database.DB_PATH), ensure_ascii=False, indent=2))
    except (sqlite3.Error, OSError) as error:
        print(json.dumps({"ok": False, "error_type": type(error).__name__, "read_only": True}))
        raise SystemExit(1)
