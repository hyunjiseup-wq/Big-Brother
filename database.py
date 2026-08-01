"""유저별 위반 점수와 검수 이력을 관리하는 SQLite 저장소."""

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

from config import STRIKE_DECAY_DAYS, STRIKE_DECAY_RATIO


load_dotenv()

DB_PATH = os.path.expandvars(os.path.expanduser(os.environ.get(
    "AUTOMOD_DB_PATH",
    str(Path(__file__).resolve().parent / "automod.db"),
)))
_BUSY_TIMEOUT_MS = 10_000
_REVIEW_RECOVERY_MIN_AGE_SECONDS = 600


@asynccontextmanager
async def _connect():
    async with aiosqlite.connect(DB_PATH, timeout=_BUSY_TIMEOUT_MS / 1000) as db:
        await db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS};")
        yield db


async def _ensure_column(db, table: str, column: str, declaration: str):
    cursor = await db.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in await cursor.fetchall()}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


async def _validate_connection_integrity(db) -> None:
    cursor = await db.execute("PRAGMA quick_check;")
    messages = [str(row[0]) for row in await cursor.fetchall()]
    if messages != ["ok"]:
        summary = "; ".join(messages[:5]) or "결과 없음"
        raise RuntimeError(f"SQLite 무결성 검사 실패: {summary}")


async def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with _connect() as db:
        # 기존 파일은 journal mode 변경이나 스키마 마이그레이션보다 먼저 검사한다.
        # 직접 `python bot.py`로 실행해 run_bot.bat을 거치지 않아도 동일하게 보호된다.
        await _validate_connection_integrity(db)
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS strikes (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                points REAL NOT NULL DEFAULT 0,
                last_violation_at REAL NOT NULL DEFAULT 0,
                last_decay_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await _ensure_column(db, "strikes", "last_decay_at", "REAL NOT NULL DEFAULT 0")
        await db.execute(
            "UPDATE strikes SET last_decay_at = last_violation_at "
            "WHERE last_decay_at = 0 AND last_violation_at > 0"
        )

        await db.execute("""
            CREATE TABLE IF NOT EXISTS violation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                message_content TEXT,
                level TEXT NOT NULL,
                reason TEXT,
                action_taken TEXT,
                provider TEXT DEFAULT 'unknown',
                needs_review INTEGER DEFAULT 0,
                review_status TEXT NOT NULL DEFAULT 'not_required',
                reviewed_at REAL,
                reviewed_by INTEGER,
                created_at REAL NOT NULL
            )
        """)
        await _ensure_column(db, "violation_log", "provider", "TEXT DEFAULT 'unknown'")
        await _ensure_column(db, "violation_log", "needs_review", "INTEGER DEFAULT 0")
        await _ensure_column(db, "violation_log", "message_id", "INTEGER")
        await _ensure_column(
            db, "violation_log", "review_status", "TEXT NOT NULL DEFAULT 'not_required'"
        )
        await _ensure_column(db, "violation_log", "reviewed_at", "REAL")
        await _ensure_column(db, "violation_log", "reviewed_by", "INTEGER")
        # card_delivered: 이 검수 건에 대해 관리자가 누를 수 있는 카드가 실제로 게시됐는지.
        # 0이면 pending이지만 카드가 없다(전송 실패 또는 배치 카드 상한 초과). 기존 행은 1로 둔다.
        await _ensure_column(db, "violation_log", "card_delivered", "INTEGER NOT NULL DEFAULT 1")
        # processing 상태는 자동으로 pending으로 되돌리지 않는다. 외부 제재 성공 직후
        # DB 확정 전에 프로세스가 종료됐을 수 있어 자동 재시도하면 중복 제재가 된다.

        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_checkpoints (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                last_message_id INTEGER,
                last_run_at REAL,
                PRIMARY KEY (guild_id, channel_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_violation_user_recent "
            "ON violation_log(guild_id, user_id, created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_violation_pending_review "
            "ON violation_log(guild_id, needs_review, created_at DESC)"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_violation_message_review "
            "ON violation_log(guild_id, message_id, review_status) "
            "WHERE message_id IS NOT NULL AND review_status IN ('pending', 'processing')"
        )

        # 관리자가 검수 카드에서 '정상(오탐)'으로 확정한 메시지 (오탐 학습용, learning.py)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS false_positives (
                guild_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                wrong_level TEXT,
                wrong_reason TEXT,
                channel_id INTEGER,
                marked_by INTEGER,
                created_at REAL NOT NULL,
                PRIMARY KEY (guild_id, content_hash)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_false_positive_recent "
            "ON false_positives(guild_id, created_at DESC)"
        )
        # v2: 오탐 허용 범위를 채널(스레드는 부모 채널) 또는 서버 전체로 구분한다.
        # scope_channel_id=0인 규칙만 서버 전체에 적용된다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS false_positive_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                scope_channel_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                wrong_level TEXT,
                wrong_reason TEXT,
                source_channel_id INTEGER,
                marked_by INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (guild_id, scope_channel_id, content_hash)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_false_positive_rule_lookup "
            "ON false_positive_rules(guild_id, scope_channel_id, content_hash, active)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_false_positive_rule_recent "
            "ON false_positive_rules(guild_id, updated_at DESC)"
        )
        await db.commit()


async def validate_database_integrity() -> None:
    """SQLite가 보고하는 구조/페이지 손상을 시작 전에 감지한다."""
    async with _connect() as db:
        await _validate_connection_integrity(db)


async def _apply_decay(db, guild_id: int, user_id: int, row):
    """경과한 감쇠 주기 수만큼 한 번만 점수를 감소시킨다."""
    points, last_violation_at, last_decay_at = row
    interval = STRIKE_DECAY_DAYS * 86400
    base_at = max(last_violation_at or 0, last_decay_at or 0)
    if not base_at:
        return points

    periods = int((time.time() - base_at) // interval)
    if periods <= 0:
        return points

    points *= STRIKE_DECAY_RATIO ** periods
    decay_at = base_at + periods * interval
    await db.execute(
        "UPDATE strikes SET points = ?, last_decay_at = ? "
        "WHERE guild_id = ? AND user_id = ?",
        (points, decay_at, guild_id, user_id),
    )
    return points


async def get_points(guild_id: int, user_id: int) -> float:
    async with _connect() as db:
        # 감쇠가 필요한 경우 쓰기가 발생하므로 add_points와 같은 쓰기 잠금을 잡는다.
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT points, last_violation_at, last_decay_at FROM strikes "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            await db.commit()
            return 0.0
        points = await _apply_decay(db, guild_id, user_id, row)
        await db.commit()
        return points


async def add_points(guild_id: int, user_id: int, points_to_add: float) -> float:
    """직렬화된 트랜잭션 안에서 점수를 원자적으로 추가한다."""
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "SELECT points, last_violation_at, last_decay_at FROM strikes "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        now = time.time()
        if row is None:
            new_points = points_to_add
            await db.execute(
                "INSERT INTO strikes "
                "(guild_id, user_id, points, last_violation_at, last_decay_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, new_points, now, now),
            )
        else:
            current_points = await _apply_decay(db, guild_id, user_id, row)
            new_points = current_points + points_to_add
            await db.execute(
                "UPDATE strikes SET points = ?, last_violation_at = ?, last_decay_at = ? "
                "WHERE guild_id = ? AND user_id = ?",
                (new_points, now, now, guild_id, user_id),
            )
        await db.commit()
        return new_points


async def reset_points(guild_id: int, user_id: int):
    async with _connect() as db:
        await db.execute(
            "DELETE FROM strikes WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        await db.commit()


async def redact_expired_violation_content(retention_days: int) -> int:
    """보존 기간이 지난 완료 기록의 메시지 원문을 비우고 변경 건수를 반환한다."""
    if retention_days <= 0:
        return 0
    cutoff = time.time() - retention_days * 86400
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE violation_log
               SET message_content = NULL
               WHERE created_at < ?
                 AND message_content IS NOT NULL
                 AND message_content != ''
                 AND review_status NOT IN ('pending', 'processing')""",
            (cutoff,),
        )
        await db.commit()
        return cursor.rowcount


async def log_violation(
    guild_id,
    user_id,
    channel_id,
    message_content,
    level,
    reason,
    action_taken,
    provider="unknown",
    needs_review=False,
    review_status="not_required",
    message_id=None,
):
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO violation_log
               (guild_id, user_id, channel_id, message_id, message_content, level, reason,
                action_taken, provider, needs_review, review_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                user_id,
                channel_id,
                message_id,
                message_content,
                level,
                reason,
                action_taken,
                provider,
                int(needs_review),
                review_status,
                time.time(),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def create_review_record(
    guild_id, user_id, channel_id, message_id, message_content,
    level, reason, action_taken, provider="unknown", card_delivered=True,
) -> int:
    """
    검수 대기(pending) 레코드를 만들고 id를 돌려준다.

    같은 메시지(message_id)에 대해 이미 대기/처리중인 검수가 있으면(예: 유저가 메시지를
    수정해 재검사된 경우) 먼저 'superseded'로 밀어내어 부분 유니크 인덱스
    (idx_violation_message_review) 충돌로 인한 IntegrityError를 방지한다.
    이렇게 하면 예전 카드의 버튼은 무력화되고(클릭 시 claim 실패) 최신 카드가 최종본이 된다.

    card_delivered=False면 pending이지만 실제 카드가 없는 상태로 기록된다
    (전송 실패 또는 배치 카드 상한 초과분). get_reviews_without_card로 조회 가능.
    """
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        if message_id is not None:
            await db.execute(
                "UPDATE violation_log SET review_status = 'superseded' "
                "WHERE guild_id = ? AND message_id = ? "
                "AND review_status IN ('pending', 'processing')",
                (guild_id, message_id),
            )
        cursor = await db.execute(
            """INSERT INTO violation_log
               (guild_id, user_id, channel_id, message_id, message_content, level, reason,
                action_taken, provider, needs_review, review_status, card_delivered, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?)""",
            (guild_id, user_id, channel_id, message_id, message_content, level, reason,
             action_taken, provider, int(card_delivered), time.time()),
        )
        await db.commit()
        return cursor.lastrowid


async def mark_review_delivery_failed(review_id: int, guild_id: int):
    """검수 카드 전송이 실패했음을 기록한다 (pending은 유지, 카드만 없음 표시)."""
    async with _connect() as db:
        await db.execute(
            "UPDATE violation_log SET card_delivered = 0 "
            "WHERE id = ? AND guild_id = ? AND review_status = 'pending'",
            (review_id, guild_id),
        )
        await db.commit()


async def get_reviews_without_card(guild_id: int, hours: int = 168, limit: int = 15):
    """카드가 게시되지 못한 채 남아 있는 pending 검수 건들을 조회한다."""
    since = time.time() - max(0, hours) * 3600
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT id, user_id, channel_id, level, reason, action_taken, created_at
               FROM violation_log
               WHERE guild_id = ? AND review_status = 'pending' AND card_delivered = 0
                 AND created_at >= ?
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, since, limit),
        )
        return await cursor.fetchall()


async def claim_review(review_id: int, guild_id: int) -> bool:
    """동일 검수 카드가 두 번 실행되지 않도록 pending 상태를 원자적으로 선점한다."""
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE violation_log SET review_status = 'processing', reviewed_at = ? "
            "WHERE id = ? AND guild_id = ? AND review_status = 'pending'",
            (time.time(), review_id, guild_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def release_review(review_id: int, guild_id: int):
    async with _connect() as db:
        await db.execute(
            "UPDATE violation_log SET review_status = 'pending', reviewed_at = NULL "
            "WHERE id = ? AND guild_id = ? AND review_status = 'processing'",
            (review_id, guild_id),
        )
        await db.commit()


async def get_stale_processing_reviews(guild_id: int, minutes: int = 10, limit: int = 10):
    """처리가 시작된 뒤 오래 완료되지 않은 검수 건을 관리자 확인용으로 조회한다."""
    before = time.time() - max(1, minutes) * 60
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT id, user_id, channel_id, level, action_taken, reviewed_at
               FROM violation_log
               WHERE guild_id = ? AND review_status = 'processing'
                 AND (reviewed_at IS NULL OR reviewed_at < ?)
               ORDER BY reviewed_at ASC LIMIT ?""",
            (guild_id, before, limit),
        )
        return await cursor.fetchall()


async def recover_processing_review(review_id: int, guild_id: int) -> bool:
    """10분 이상 중단된 processing 건만 관리자가 다시 pending으로 돌린다."""
    before = time.time() - _REVIEW_RECOVERY_MIN_AGE_SECONDS
    async with _connect() as db:
        cursor = await db.execute(
            "UPDATE violation_log SET review_status = 'pending', reviewed_at = NULL "
            "WHERE id = ? AND guild_id = ? AND review_status = 'processing' "
            "AND (reviewed_at IS NULL OR reviewed_at < ?)",
            (review_id, guild_id, before),
        )
        await db.commit()
        return cursor.rowcount == 1


async def resolve_review(
    review_id: int,
    guild_id: int,
    status: str,
    reviewer_id: int,
    action_taken: str,
):
    if status not in {"confirmed", "false_positive"}:
        raise ValueError(f"알 수 없는 검수 상태: {status}")
    async with _connect() as db:
        await db.execute(
            """UPDATE violation_log
               SET review_status = ?, reviewed_at = ?, reviewed_by = ?,
                   action_taken = ?, needs_review = 0
               WHERE id = ? AND guild_id = ? AND review_status = 'processing'""",
            (status, time.time(), reviewer_id, action_taken, review_id, guild_id),
        )
        await db.commit()


async def get_checkpoint(guild_id: int, channel_id: int):
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT last_message_id FROM channel_checkpoints WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_checkpoint(guild_id: int, channel_id: int, last_message_id: int):
    async with _connect() as db:
        await db.execute(
            """INSERT INTO channel_checkpoints (guild_id, channel_id, last_message_id, last_run_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, channel_id)
               DO UPDATE SET last_message_id = excluded.last_message_id,
                             last_run_at = excluded.last_run_at""",
            (guild_id, channel_id, last_message_id, time.time()),
        )
        await db.commit()


async def get_pending_reviews(guild_id: int, hours: int = 72, limit: int = 20):
    since = time.time() - max(0, hours) * 3600
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT user_id, level, reason, action_taken, provider, created_at
               FROM violation_log
               WHERE guild_id = ? AND needs_review = 1
                 AND created_at >= ? AND review_status != 'false_positive'
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, since, limit),
        )
        return await cursor.fetchall()


async def get_recent_violations(guild_id: int, user_id: int, limit: int = 10):
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT level, reason, action_taken, provider, created_at
               FROM violation_log
               WHERE guild_id = ? AND user_id = ? AND review_status != 'false_positive'
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, user_id, limit),
        )
        return await cursor.fetchall()


async def get_violation_content(review_id: int, guild_id: int):
    """검수 카드가 가리키는 위반 로그의 원문 전체를 가져온다 (임베드는 500자 절단본)."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT message_content FROM violation_log WHERE id = ? AND guild_id = ?",
            (review_id, guild_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


# ── 오탐 학습 (learning.py에서 사용) ─────────────────────────────────

async def add_false_positive(guild_id, content_hash, content, wrong_level,
                             wrong_reason, channel_id, marked_by):
    """오탐 확정 메시지를 저장한다. 같은 내용이 다시 확정되면 최신 정보로 갱신."""
    async with _connect() as db:
        await db.execute(
            """INSERT INTO false_positives
               (guild_id, content_hash, content, wrong_level, wrong_reason,
                channel_id, marked_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, content_hash) DO UPDATE SET
                   wrong_level = excluded.wrong_level,
                   wrong_reason = excluded.wrong_reason,
                   channel_id = excluded.channel_id,
                   marked_by = excluded.marked_by,
                   created_at = excluded.created_at""",
            (guild_id, content_hash, content, wrong_level, wrong_reason,
             channel_id, marked_by, time.time()),
        )
        await db.commit()


async def get_recent_false_positives(guild_id: int, limit: int = 15):
    """프롬프트에 예시로 넣을 최근 오탐 사례 (최신순)."""
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT content, wrong_level, wrong_reason FROM false_positives
               WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, limit),
        )
        return await cursor.fetchall()


async def get_all_false_positive_hashes():
    """완전 일치 차단용 해시 전체 (시작 시 메모리에 로드)."""
    async with _connect() as db:
        cursor = await db.execute("SELECT guild_id, content_hash FROM false_positives")
        return await cursor.fetchall()


# ── 범위 지정 오탐 학습(v2) ─────────────────────────────────────────

async def upsert_false_positive_rule(guild_id: int, scope_channel_id: int,
                                     content_hash: str, content: str,
                                     wrong_level: str | None, wrong_reason: str | None,
                                     source_channel_id: int | None,
                                     marked_by: int | None) -> int:
    now = time.time()
    async with _connect() as db:
        await db.execute(
            """INSERT INTO false_positive_rules
               (guild_id, scope_channel_id, content_hash, content, wrong_level, wrong_reason,
                source_channel_id, marked_by, active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(guild_id, scope_channel_id, content_hash) DO UPDATE SET
                   content = excluded.content,
                   wrong_level = excluded.wrong_level,
                   wrong_reason = excluded.wrong_reason,
                   source_channel_id = excluded.source_channel_id,
                   marked_by = excluded.marked_by,
                   active = 1,
                   updated_at = excluded.updated_at""",
            (guild_id, scope_channel_id, content_hash, content, wrong_level, wrong_reason,
             source_channel_id, marked_by, now, now),
        )
        cursor = await db.execute(
            """SELECT id FROM false_positive_rules
               WHERE guild_id = ? AND scope_channel_id = ? AND content_hash = ?""",
            (guild_id, scope_channel_id, content_hash),
        )
        row = await cursor.fetchone()
        await db.commit()
        return int(row[0])


async def resolve_review_as_false_positive(review_id: int, guild_id: int,
                                           scope_channel_id: int, content_hash: str,
                                           stored_content: str,
                                           reviewer_id: int, action_taken: str):
    """검수 완료와 오탐 규칙 저장을 한 트랜잭션으로 처리한다."""
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """SELECT channel_id, message_content, level, reason
               FROM violation_log
               WHERE id = ? AND guild_id = ? AND review_status = 'processing'""",
            (review_id, guild_id),
        )
        row = await cursor.fetchone()
        if row is None or not row[1] or not row[1].strip():
            await db.rollback()
            return None

        source_channel_id, content, wrong_level, wrong_reason = row
        now = time.time()
        await db.execute(
            """INSERT INTO false_positive_rules
               (guild_id, scope_channel_id, content_hash, content, wrong_level, wrong_reason,
                source_channel_id, marked_by, active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(guild_id, scope_channel_id, content_hash) DO UPDATE SET
                   content = excluded.content,
                   wrong_level = excluded.wrong_level,
                   wrong_reason = excluded.wrong_reason,
                   source_channel_id = excluded.source_channel_id,
                   marked_by = excluded.marked_by,
                   active = 1,
                   updated_at = excluded.updated_at""",
            (guild_id, scope_channel_id, content_hash, stored_content, wrong_level, wrong_reason,
             source_channel_id, reviewer_id, now, now),
        )
        rule_cursor = await db.execute(
            """SELECT id FROM false_positive_rules
               WHERE guild_id = ? AND scope_channel_id = ? AND content_hash = ?""",
            (guild_id, scope_channel_id, content_hash),
        )
        rule_id = int((await rule_cursor.fetchone())[0])
        updated = await db.execute(
            """UPDATE violation_log
               SET review_status = 'false_positive', reviewed_at = ?, reviewed_by = ?,
                   action_taken = ?, needs_review = 0
               WHERE id = ? AND guild_id = ? AND review_status = 'processing'""",
            (now, reviewer_id, action_taken, review_id, guild_id),
        )
        if updated.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        return {"id": rule_id, "content": content}


async def get_all_false_positive_rule_keys(include_inactive: bool = False):
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT guild_id, scope_channel_id, content_hash
               FROM false_positive_rules WHERE active = 1 OR ? = 1""",
            (int(include_inactive),),
        )
        return await cursor.fetchall()


async def get_recent_false_positive_rules(guild_id: int, scope_channel_id: int,
                                          limit: int = 15):
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT id, scope_channel_id, content, wrong_level, updated_at
               FROM false_positive_rules
               WHERE guild_id = ? AND active = 1
                 AND scope_channel_id IN (0, ?)
               ORDER BY updated_at DESC LIMIT ?""",
            (guild_id, scope_channel_id, limit),
        )
        return await cursor.fetchall()


async def list_false_positive_rules(guild_id: int, limit: int = 20):
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT id, scope_channel_id, content, wrong_level, marked_by, updated_at
               FROM false_positive_rules
               WHERE guild_id = ? AND active = 1
               ORDER BY updated_at DESC LIMIT ?""",
            (guild_id, limit),
        )
        return await cursor.fetchall()


async def deactivate_false_positive_rule(guild_id: int, rule_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE false_positive_rules SET active = 0, updated_at = ?
               WHERE id = ? AND guild_id = ? AND active = 1""",
            (time.time(), rule_id, guild_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_false_positive_backfill_rows():
    """기존 학습 테이블과 과거 오탐 검수 이력을 v2로 옮길 원본을 반환한다."""
    async with _connect() as db:
        legacy = await (await db.execute(
            """SELECT guild_id, channel_id, content, wrong_level, wrong_reason, marked_by
               FROM false_positives WHERE content IS NOT NULL AND trim(content) != ''"""
        )).fetchall()
        history = await (await db.execute(
            """SELECT guild_id, channel_id, message_content, level, reason, reviewed_by
               FROM violation_log
               WHERE review_status = 'false_positive'
                 AND message_content IS NOT NULL AND trim(message_content) != ''"""
        )).fetchall()
        return legacy + history
