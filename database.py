"""유저별 위반 점수와 검수 이력을 관리하는 SQLite 저장소."""

import asyncio
import datetime
import os
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

from config import STRIKE_DECAY_DAYS, STRIKE_DECAY_RATIO
from language_detection import detect_language_group


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


async def _enqueue_kpi_sync(db, review_id: int, guild_id: int, now: float | None = None):
    """현재 트랜잭션 안에서 KPI 변경을 outbox에 멱등 등록한다."""
    updated_at = time.time() if now is None else now
    await db.execute(
        """INSERT INTO kpi_sync_outbox
           (review_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
           VALUES (?, ?, 0, 0, NULL, ?)
           ON CONFLICT(review_id) DO UPDATE SET
               guild_id = excluded.guild_id,
               attempts = 0,
               next_attempt_at = 0,
               last_error = NULL,
               updated_at = excluded.updated_at""",
        (review_id, guild_id, updated_at),
    )


async def _mark_kpi_snapshots_dirty_for_review(db, review_id: int,
                                                guild_id: int) -> None:
    """현재 트랜잭션에서 해당 카드가 속한 기존 기간 스냅샷만 무효화한다."""
    await db.execute(
        """UPDATE kpi_period_snapshots SET dirty = 1
           WHERE guild_id = ? AND EXISTS (
               SELECT 1 FROM violation_log v
               WHERE v.id = ? AND v.guild_id = ?
                 AND v.created_at >= kpi_period_snapshots.period_start
                 AND v.created_at < kpi_period_snapshots.period_end
           )""",
        (guild_id, review_id, guild_id),
    )


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
        await _ensure_column(db, "violation_log", "language_group", "TEXT NOT NULL DEFAULT 'und'")
        await _ensure_column(db, "violation_log", "rule_violated", "TEXT DEFAULT '-'")
        await _ensure_column(
            db, "violation_log", "detection_source", "TEXT NOT NULL DEFAULT 'legacy'"
        )
        await _ensure_column(db, "violation_log", "channel_name", "TEXT")
        await _ensure_column(db, "violation_log", "channel_group", "TEXT")
        # 검수 시점에 스레드가 캐시에서 사라져도 오탐 학습 범위를 잃지 않도록,
        # 감지 당시 계산한 부모 채널(일반 채널이면 자기 자신)을 별도로 보존한다.
        await _ensure_column(db, "violation_log", "learning_scope_channel_id", "INTEGER")
        # card_delivered: 이 검수 건에 대해 관리자가 누를 수 있는 카드가 실제로 게시됐는지.
        # 0이면 pending이지만 카드가 없다(전송 실패 또는 배치 카드 상한 초과). 기존 행은 1로 둔다.
        await _ensure_column(db, "violation_log", "card_delivered", "INTEGER NOT NULL DEFAULT 1")
        # 관리자 확정 근거. 원문/AI 사유와 분리해 인수인계와 학습 설명에 사용한다.
        await _ensure_column(db, "violation_log", "review_note", "TEXT")
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
        # 월간/분기/연간 KPI는 길드·기간을 먼저 제한한 뒤 상태/채널로 묶는다.
        # 메시지 원문을 읽지 않고 장기 메타데이터만 집계할 수 있도록 실제 조회 순서에 맞춘다.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_violation_kpi_period "
            "ON violation_log(guild_id, created_at, review_status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_violation_kpi_channel_period "
            "ON violation_log(guild_id, channel_id, created_at)"
        )

        # AI 전량 장애 메시지는 원문을 중복 저장하지 않고 Discord 식별자만 보관한다.
        # 재시작 후에도 Discord에서 최신 원문을 다시 읽어 판단할 수 있다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moderation_retry_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                last_failure TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(guild_id, message_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_moderation_retry_due "
            "ON moderation_retry_queue(next_attempt_at, id)"
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
        await _ensure_column(db, "false_positive_rules", "learning_explanation", "TEXT")

        # Only administrator-confirmed outcomes become training labels. Message text is
        # deliberately not duplicated here; it remains subject to violation_log retention.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moderation_labels (
                review_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                verdict TEXT NOT NULL CHECK (verdict IN ('normal', 'violation')),
                corrected_level TEXT NOT NULL,
                marked_by INTEGER,
                created_at REAL NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_moderation_label_recent "
            "ON moderation_labels(guild_id, created_at DESC)"
        )

        # 사이트가 잠시 끊겨도 KPI 갱신을 잃지 않는 영속 outbox. review_id만 보관하며
        # 전송 시 violation_log에서 비식별 메타데이터를 다시 읽는다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kpi_sync_outbox (
                review_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_kpi_sync_due "
            "ON kpi_sync_outbox(next_attempt_at, review_id)"
        )

        # 관리자 인수인계용 제재 원장. 공개 KPI와 달리 사용자·처리자·사유를 보존하며,
        # 외부 사이트에서는 비밀번호로 보호된 스태프 화면에서만 조회한다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sanction_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_display TEXT NOT NULL,
                action_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                issued_at REAL NOT NULL,
                expires_at REAL,
                released_at REAL,
                issued_by_id INTEGER,
                issued_by_display TEXT,
                released_by_id INTEGER,
                released_by_display TEXT,
                release_reason TEXT,
                review_id INTEGER,
                dedupe_key TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE (guild_id, dedupe_key)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sanction_user_history "
            "ON sanction_records(guild_id, user_id, issued_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sanction_status_history "
            "ON sanction_records(guild_id, status, issued_at DESC)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sanction_sync_outbox (
                sanction_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sanction_sync_due "
            "ON sanction_sync_outbox(next_attempt_at, sanction_id)"
        )
        # Discord 감사 로그 실시간 이벤트가 누락되거나 봇이 잠시 재연결돼도
        # 마지막 확인 지점 이후의 관리자·외부 봇 제재를 다시 읽기 위한 커서다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS discord_audit_cursors (
                guild_id INTEGER PRIMARY KEY,
                last_entry_id INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        # 배치 감사의 커버리지 KPI. 감사 리포트 원문을 다시 파싱하지 않고 실행 단위의
        # 검토량·의심 감지·실패 채널 수를 장기 보존한다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_run_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                backend TEXT NOT NULL,
                reviewed_messages INTEGER NOT NULL,
                flagged_messages INTEGER NOT NULL,
                failed_channels INTEGER NOT NULL,
                target_channels INTEGER NOT NULL,
                report_path TEXT,
                created_at REAL NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_run_metrics_period "
            "ON audit_run_metrics(guild_id, created_at)"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_run_metrics_report "
            "ON audit_run_metrics(guild_id, report_path) WHERE report_path IS NOT NULL"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_kpi_sync_outbox (
                audit_run_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at REAL NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_kpi_sync_due "
            "ON audit_kpi_sync_outbox(next_attempt_at, audit_run_id)"
        )

        # 종료된 기간의 집계를 JSON으로 고정해 장기 조회 비용을 일정하게 유지한다.
        # 검수 결과가 뒤늦게 바뀌면 dirty=1로 표시해 그 기간만 다시 계산한다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kpi_period_snapshots (
                guild_id INTEGER NOT NULL,
                period_type TEXT NOT NULL,
                period_key TEXT NOT NULL,
                period_start REAL NOT NULL,
                period_end REAL NOT NULL,
                summary_json TEXT,
                source_rows INTEGER NOT NULL DEFAULT 0,
                dirty INTEGER NOT NULL DEFAULT 1,
                finalized INTEGER NOT NULL DEFAULT 0,
                refreshed_at REAL,
                PRIMARY KEY (guild_id, period_type, period_key)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_kpi_period_snapshot_dirty "
            "ON kpi_period_snapshots(guild_id, dirty, period_end)"
        )

        # 봇 재시작·Discord 재연결에도 같은 월/분기/연간 정산을 두 번 보내지 않는다.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kpi_report_deliveries (
                guild_id INTEGER NOT NULL,
                period_type TEXT NOT NULL,
                period_key TEXT NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                delivered_at REAL NOT NULL,
                PRIMARY KEY (guild_id, period_type, period_key)
            )
        """)
        await db.execute("PRAGMA optimize")
        await db.commit()


async def validate_database_integrity() -> None:
    """SQLite가 보고하는 구조/페이지 손상을 시작 전에 감지한다."""
    async with _connect() as db:
        await _validate_connection_integrity(db)


def _backup_database_sync(source: Path, destination: Path) -> None:
    partial = destination.with_suffix(destination.suffix + ".partial")
    try:
        # Connection의 일반 context manager는 commit/rollback만 하고 close하지 않는다.
        # Windows에서는 열린 파일을 rename할 수 없으므로 closing으로 핸들을 먼저 닫는다.
        with closing(sqlite3.connect(source)) as source_db:
            with closing(sqlite3.connect(partial)) as backup_db:
                source_db.backup(backup_db)
                result = backup_db.execute("PRAGMA quick_check;").fetchall()
                if result != [("ok",)]:
                    raise RuntimeError("생성된 SQLite 백업의 무결성 검사에 실패했습니다.")
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


async def create_database_backup(output_dir: str | os.PathLike | None = None) -> Path:
    """실행 중인 WAL DB도 일관되게 복사하는 명시적 수동 백업을 만든다."""
    await validate_database_integrity()
    source = Path(DB_PATH).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"백업할 DB 파일이 없습니다: {source}")
    directory = Path(output_dir or Path(__file__).resolve().parent / "backups").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    destination = directory / f"automod_backup_{stamp}.db"
    await asyncio.to_thread(_backup_database_sync, source, destination)
    return destination


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
                action_taken, provider, needs_review, review_status, language_group, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                detect_language_group(message_content),
                time.time(),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def enqueue_moderation_retry(
    guild_id: int,
    channel_id: int,
    message_id: int,
    failure_category: str,
    delay_seconds: float,
) -> None:
    """AI 전량 장애 메시지를 중복 없이 내구성 보류 큐에 넣는다."""
    now = time.time()
    next_attempt_at = now + max(0.0, delay_seconds)
    async with _connect() as db:
        await db.execute(
            """INSERT INTO moderation_retry_queue
               (guild_id, channel_id, message_id, attempts, next_attempt_at,
                last_failure, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?)
               ON CONFLICT(guild_id, message_id) DO UPDATE SET
                   channel_id = excluded.channel_id,
                   next_attempt_at = MIN(moderation_retry_queue.next_attempt_at,
                                         excluded.next_attempt_at),
                   last_failure = excluded.last_failure,
                   updated_at = excluded.updated_at""",
            (guild_id, channel_id, message_id, next_attempt_at,
             failure_category[:500], now, now),
        )
        await db.commit()


async def get_due_moderation_retries(limit: int = 10):
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT id, guild_id, channel_id, message_id, attempts, last_failure
               FROM moderation_retry_queue
               WHERE next_attempt_at <= ?
               ORDER BY next_attempt_at ASC, id ASC
               LIMIT ?""",
            (time.time(), max(1, limit)),
        )
        return await cursor.fetchall()


async def reschedule_moderation_retry(
    retry_id: int,
    attempts: int,
    failure_category: str,
    delay_seconds: float,
) -> None:
    now = time.time()
    async with _connect() as db:
        await db.execute(
            """UPDATE moderation_retry_queue
               SET attempts = ?, next_attempt_at = ?, last_failure = ?, updated_at = ?
               WHERE id = ?""",
            (attempts, now + max(0.0, delay_seconds),
             failure_category[:500], now, retry_id),
        )
        await db.commit()


async def delete_moderation_retry(retry_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM moderation_retry_queue WHERE id = ?", (retry_id,))
        await db.commit()


async def delete_moderation_retry_for_message(guild_id: int, message_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            "DELETE FROM moderation_retry_queue WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        await db.commit()


async def count_moderation_retries(guild_id: int | None = None) -> int:
    async with _connect() as db:
        if guild_id is None:
            cursor = await db.execute("SELECT COUNT(*) FROM moderation_retry_queue")
        else:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM moderation_retry_queue WHERE guild_id = ?",
                (guild_id,),
            )
        row = await cursor.fetchone()
        return int(row[0])


# ── KPI 집계·대시보드 동기화 ────────────────────────────────────────

async def record_sanction(
    guild_id: int,
    user_id: int,
    user_display: str,
    action_type: str,
    reason: str,
    source: str,
    dedupe_key: str,
    *,
    issued_by_id: int | None = None,
    issued_by_display: str | None = None,
    issued_at: float | None = None,
    expires_at: float | None = None,
    review_id: int | None = None,
) -> int:
    """제재 적용 사실과 사이트 동기화 항목을 같은 트랜잭션에 기록한다."""
    now = time.time()
    issued = now if issued_at is None else float(issued_at)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """INSERT OR IGNORE INTO sanction_records
               (guild_id, user_id, user_display, action_type, reason, source, status,
                issued_at, expires_at, issued_by_id, issued_by_display, review_id,
                dedupe_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(guild_id), int(user_id), str(user_display)[:120],
                str(action_type)[:32], str(reason)[:1000], str(source)[:32],
                issued, expires_at, issued_by_id,
                str(issued_by_display)[:120] if issued_by_display else None,
                review_id, str(dedupe_key)[:160], now, now,
            ),
        )
        row = await (await db.execute(
            "SELECT id FROM sanction_records WHERE guild_id = ? AND dedupe_key = ?",
            (int(guild_id), str(dedupe_key)[:160]),
        )).fetchone()
        sanction_id = int(row[0])
        if cursor.rowcount == 1:
            await db.execute(
                """INSERT OR REPLACE INTO sanction_sync_outbox
                   (sanction_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
                   VALUES (?, ?, 0, 0, NULL, ?)""",
                (sanction_id, int(guild_id), now),
            )
        await db.commit()
        return sanction_id


async def import_sanction_record(
    guild_id: int,
    user_id: int,
    user_display: str,
    action_type: str,
    reason: str,
    dedupe_key: str,
    *,
    status: str,
    issued_at: float,
    expires_at: float | None = None,
    released_at: float | None = None,
    issued_by_id: int | None = None,
    issued_by_display: str | None = None,
    released_by_id: int | None = None,
    released_by_display: str | None = None,
    release_reason: str | None = None,
) -> tuple[int, bool]:
    """외부 제재 이력을 원형대로 한 번만 가져오고 사이트 동기화 큐에 등록한다."""
    if status not in {"active", "released", "expired"}:
        raise ValueError(f"지원하지 않는 가져오기 상태: {status}")
    now = time.time()
    key = str(dedupe_key)[:160]
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        inserted = await db.execute(
            """INSERT OR IGNORE INTO sanction_records
               (guild_id, user_id, user_display, action_type, reason, source, status,
                issued_at, expires_at, released_at, issued_by_id, issued_by_display,
                released_by_id, released_by_display, release_reason, review_id,
                dedupe_key, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'legacy_import', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       NULL, ?, ?, ?)""",
            (
                int(guild_id), int(user_id), str(user_display)[:120],
                str(action_type)[:32], str(reason)[:1000], status,
                float(issued_at), expires_at, released_at, issued_by_id,
                str(issued_by_display)[:120] if issued_by_display else None,
                released_by_id,
                str(released_by_display)[:120] if released_by_display else None,
                str(release_reason)[:1000] if release_reason else None,
                key, now, now,
            ),
        )
        row = await (await db.execute(
            "SELECT id FROM sanction_records WHERE guild_id = ? AND dedupe_key = ?",
            (int(guild_id), key),
        )).fetchone()
        if row is None:
            await db.rollback()
            raise RuntimeError("가져온 제재 기록 ID를 확인하지 못했습니다.")
        sanction_id = int(row[0])
        was_inserted = inserted.rowcount == 1
        if was_inserted:
            await db.execute(
                """INSERT OR REPLACE INTO sanction_sync_outbox
                   (sanction_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
                   VALUES (?, ?, 0, 0, NULL, ?)""",
                (sanction_id, int(guild_id), now),
            )
        await db.commit()
        return sanction_id, was_inserted


async def count_sanction_sync_pending(guild_id: int | None = None) -> int:
    """스태프 원장 사이트로 아직 전달되지 않은 제재 기록 수."""
    async with _connect() as db:
        if guild_id is None:
            cursor = await db.execute("SELECT COUNT(*) FROM sanction_sync_outbox")
        else:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM sanction_sync_outbox WHERE guild_id = ?",
                (int(guild_id),),
            )
        return int((await cursor.fetchone())[0])


async def get_discord_audit_cursor(guild_id: int) -> int | None:
    """마지막으로 보충 확인을 마친 Discord 감사 로그 항목 ID."""
    async with _connect() as db:
        row = await (await db.execute(
            "SELECT last_entry_id FROM discord_audit_cursors WHERE guild_id = ?",
            (int(guild_id),),
        )).fetchone()
        return int(row[0]) if row else None


async def advance_discord_audit_cursor(guild_id: int, entry_id: int) -> None:
    """감사 로그 커서를 뒤로 이동시키지 않고 원자적으로 전진시킨다."""
    now = time.time()
    async with _connect() as db:
        await db.execute(
            """INSERT INTO discord_audit_cursors (guild_id, last_entry_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                 last_entry_id = MAX(last_entry_id, excluded.last_entry_id),
                 updated_at = CASE WHEN excluded.last_entry_id > last_entry_id
                                   THEN excluded.updated_at ELSE updated_at END""",
            (int(guild_id), int(entry_id), now),
        )
        await db.commit()


async def release_active_sanctions(
    guild_id: int,
    user_id: int,
    action_type: str,
    release_reason: str,
    *,
    released_by_id: int | None = None,
    released_by_display: str | None = None,
    released_at: float | None = None,
    status: str = "released",
    limit: int | None = None,
) -> list[int]:
    """활성 제재를 해제/만료 처리하고 변경분을 사이트 동기화 대기열에 넣는다."""
    if status not in {"released", "expired"}:
        raise ValueError(f"지원하지 않는 제재 종료 상태: {status}")
    now = time.time() if released_at is None else float(released_at)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        sql = (
            "SELECT id FROM sanction_records WHERE guild_id = ? AND user_id = ? "
            "AND action_type = ? AND status = 'active' ORDER BY issued_at DESC"
        )
        params: list[object] = [int(guild_id), int(user_id), str(action_type)[:32]]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        ids = [int(row[0]) for row in await (await db.execute(sql, params)).fetchall()]
        if not ids:
            await db.rollback()
            return []
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"""UPDATE sanction_records SET status = ?, released_at = ?,
                   released_by_id = ?, released_by_display = ?, release_reason = ?,
                   updated_at = ? WHERE id IN ({placeholders})""",
            (
                status, now, released_by_id,
                str(released_by_display)[:120] if released_by_display else None,
                str(release_reason)[:1000], time.time(), *ids,
            ),
        )
        await db.executemany(
            """INSERT OR REPLACE INTO sanction_sync_outbox
               (sanction_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
               VALUES (?, ?, 0, 0, NULL, ?)""",
            [(sanction_id, int(guild_id), time.time()) for sanction_id in ids],
        )
        await db.commit()
        return ids


async def expire_elapsed_timeouts(now: float | None = None) -> int:
    """Discord 만료 이벤트가 오지 않아도 종료 시각이 지난 타임아웃을 확정한다."""
    current = time.time() if now is None else float(now)
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        ids = [int(row[0]) for row in await (await db.execute(
            """SELECT id FROM sanction_records
               WHERE action_type = 'TIMEOUT' AND status = 'active'
                 AND expires_at IS NOT NULL AND expires_at <= ?""",
            (current,),
        )).fetchall()]
        if not ids:
            await db.rollback()
            return 0
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"""UPDATE sanction_records SET status = 'expired', released_at = expires_at,
                   release_reason = '설정된 타임아웃 기간 만료', updated_at = ?
               WHERE id IN ({placeholders})""",
            (time.time(), *ids),
        )
        await db.executemany(
            """INSERT OR REPLACE INTO sanction_sync_outbox
               (sanction_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
               SELECT id, guild_id, 0, 0, NULL, ? FROM sanction_records WHERE id = ?""",
            [(time.time(), sanction_id) for sanction_id in ids],
        )
        await db.commit()
        return len(ids)


async def get_sanction_history(guild_id: int, user_id: int | None = None,
                               limit: int = 20) -> list[dict]:
    where = "WHERE guild_id = ?"
    params: list[object] = [int(guild_id)]
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(int(user_id))
    params.append(max(1, min(int(limit), 100)))
    async with _connect() as db:
        cursor = await db.execute(
            f"""SELECT id, user_id, user_display, action_type, reason, source, status,
                       issued_at, expires_at, released_at, issued_by_id,
                       issued_by_display, released_by_id, released_by_display,
                       release_reason
                FROM sanction_records {where}
                ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                         issued_at DESC LIMIT ?""",
            params,
        )
        columns = (
            "sanction_id", "user_id", "user_display", "action_type", "reason",
            "source", "status", "issued_at", "expires_at", "released_at",
            "issued_by_id", "issued_by_display", "released_by_id",
            "released_by_display", "release_reason",
        )
        return [dict(zip(columns, row)) for row in await cursor.fetchall()]


_SANCTION_SYNC_COLUMNS = (
    "sanction_id", "guild_id", "user_id", "user_display", "action_type",
    "reason", "source", "status", "issued_at", "expires_at", "released_at",
    "issued_by_id", "issued_by_display", "released_by_id", "released_by_display",
    "release_reason", "dedupe_key", "review_id", "sync_attempts",
)


async def backfill_sanction_sync_outbox(guild_id: int) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO sanction_sync_outbox
               (sanction_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
               SELECT id, guild_id, 0, 0, NULL, ? FROM sanction_records
               WHERE guild_id = ?""",
            (time.time(), int(guild_id)),
        )
        await db.commit()
        return max(0, cursor.rowcount)


async def get_due_sanction_sync_records(limit: int = 50) -> list[dict]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT s.id, s.guild_id, s.user_id, s.user_display, s.action_type,
                      s.reason, s.source, s.status, s.issued_at, s.expires_at,
                      s.released_at, s.issued_by_id, s.issued_by_display,
                      s.released_by_id, s.released_by_display, s.release_reason,
                      s.dedupe_key, s.review_id, o.attempts
               FROM sanction_sync_outbox o JOIN sanction_records s
                 ON s.id = o.sanction_id AND s.guild_id = o.guild_id
               WHERE o.next_attempt_at <= ?
               ORDER BY o.next_attempt_at, o.sanction_id LIMIT ?""",
            (time.time(), max(1, int(limit))),
        )
        return [dict(zip(_SANCTION_SYNC_COLUMNS, row)) for row in await cursor.fetchall()]


async def mark_sanction_sync_complete(sanction_ids: list[int]) -> int:
    if not sanction_ids:
        return 0
    placeholders = ",".join("?" for _ in sanction_ids)
    async with _connect() as db:
        cursor = await db.execute(
            f"DELETE FROM sanction_sync_outbox WHERE sanction_id IN ({placeholders})",
            tuple(map(int, sanction_ids)),
        )
        await db.commit()
        return cursor.rowcount


async def reschedule_sanction_sync(sanction_ids: list[int], error_category: str,
                                   delay_seconds: float) -> None:
    if not sanction_ids:
        return
    next_attempt = time.time() + max(1.0, float(delay_seconds))
    async with _connect() as db:
        await db.executemany(
            """UPDATE sanction_sync_outbox SET attempts = attempts + 1,
                   next_attempt_at = ?, last_error = ?, updated_at = ?
               WHERE sanction_id = ?""",
            [
                (next_attempt, str(error_category)[:100], time.time(), int(sanction_id))
                for sanction_id in sanction_ids
            ],
        )
        await db.commit()


_KPI_ROW_COLUMNS = (
    "review_id", "guild_id", "channel_id", "channel_name", "channel_group",
    "level", "reason", "rule_violated",
    "provider", "review_status", "card_delivered", "language_group",
    "detection_source", "created_at", "reviewed_at", "sync_attempts",
)


def _kpi_row_dict(row) -> dict:
    return dict(zip(_KPI_ROW_COLUMNS, row))


async def backfill_kpi_sync_outbox(guild_id: int) -> int:
    """기존 검수 기록도 최초 사이트 연결 때 한 번에 안전하게 동기화 대상으로 만든다."""
    now = time.time()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO kpi_sync_outbox
               (review_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
               SELECT id, guild_id, 0, 0, NULL, ? FROM violation_log
               WHERE guild_id = ? AND review_status <> 'not_required'""",
            (now, guild_id),
        )
        await db.commit()
        return max(0, cursor.rowcount)


async def get_due_kpi_sync_records(limit: int = 50) -> list[dict]:
    """동기화할 비식별 KPI 메타데이터를 반환한다. 원문·유저·메시지 ID는 포함하지 않는다."""
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT v.id, v.guild_id, v.channel_id, v.channel_name, v.channel_group,
                      v.level, v.reason,
                      COALESCE(v.rule_violated, '-'), v.provider, v.review_status,
                      v.card_delivered, v.language_group,
                      COALESCE(v.detection_source, 'legacy'), v.created_at, v.reviewed_at,
                      o.attempts
               FROM kpi_sync_outbox AS o
               JOIN violation_log AS v ON v.id = o.review_id AND v.guild_id = o.guild_id
               WHERE o.next_attempt_at <= ?
               ORDER BY o.next_attempt_at, o.review_id
               LIMIT ?""",
            (time.time(), max(1, limit)),
        )
        return [_kpi_row_dict(row) for row in await cursor.fetchall()]


async def mark_kpi_sync_complete(review_ids: list[int]) -> int:
    if not review_ids:
        return 0
    placeholders = ",".join("?" for _ in review_ids)
    async with _connect() as db:
        cursor = await db.execute(
            f"DELETE FROM kpi_sync_outbox WHERE review_id IN ({placeholders})",
            tuple(int(review_id) for review_id in review_ids),
        )
        await db.commit()
        return cursor.rowcount


async def reschedule_kpi_sync(review_ids: list[int], error_category: str,
                              delay_seconds: float) -> None:
    if not review_ids:
        return
    next_attempt = time.time() + max(1.0, delay_seconds)
    async with _connect() as db:
        await db.executemany(
            """UPDATE kpi_sync_outbox
               SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?, updated_at = ?
               WHERE review_id = ?""",
            [
                (next_attempt, str(error_category)[:100], time.time(), int(review_id))
                for review_id in review_ids
            ],
        )
        await db.commit()


async def count_kpi_sync_pending(guild_id: int | None = None) -> int:
    async with _connect() as db:
        if guild_id is None:
            cursor = await db.execute(
                "SELECT (SELECT COUNT(*) FROM kpi_sync_outbox) + "
                "(SELECT COUNT(*) FROM audit_kpi_sync_outbox) + "
                "(SELECT COUNT(*) FROM sanction_sync_outbox)"
            )
        else:
            cursor = await db.execute(
                "SELECT (SELECT COUNT(*) FROM kpi_sync_outbox WHERE guild_id = ?) + "
                "(SELECT COUNT(*) FROM audit_kpi_sync_outbox WHERE guild_id = ?) + "
                "(SELECT COUNT(*) FROM sanction_sync_outbox WHERE guild_id = ?)",
                (guild_id, guild_id, guild_id),
            )
        row = await cursor.fetchone()
        return int(row[0])


async def get_kpi_rows(guild_id: int, start_at: float, end_at: float) -> list[dict]:
    """기간 KPI 집계에 필요한 메타데이터만 조회한다."""
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT id, guild_id, channel_id, channel_name, channel_group, level, reason,
                      COALESCE(rule_violated, '-'), provider, review_status,
                      card_delivered, language_group,
                      COALESCE(detection_source, 'legacy'), created_at, reviewed_at
               FROM violation_log
               WHERE guild_id = ? AND created_at >= ? AND created_at < ?
                 AND review_status <> 'not_required'
               ORDER BY created_at""",
            (guild_id, start_at, end_at),
        )
        return [_kpi_row_dict(row) for row in await cursor.fetchall()]


async def get_kpi_operational_counts(guild_id: int, now: float | None = None) -> dict:
    current = time.time() if now is None else now
    async with _connect() as db:
        pending_24h = int((await (await db.execute(
            "SELECT COUNT(*) FROM violation_log WHERE guild_id = ? "
            "AND review_status IN ('pending', 'processing') AND created_at < ?",
            (guild_id, current - 86400),
        )).fetchone())[0])
        pending_72h = int((await (await db.execute(
            "SELECT COUNT(*) FROM violation_log WHERE guild_id = ? "
            "AND review_status IN ('pending', 'processing') AND created_at < ?",
            (guild_id, current - 3 * 86400),
        )).fetchone())[0])
        active_learning = int((await (await db.execute(
            "SELECT COUNT(*) FROM false_positive_rules WHERE guild_id = ? AND active = 1",
            (guild_id,),
        )).fetchone())[0])
        retry_queue = int((await (await db.execute(
            "SELECT COUNT(*) FROM moderation_retry_queue WHERE guild_id = ?",
            (guild_id,),
        )).fetchone())[0])
        kpi_outbox = int((await (await db.execute(
            "SELECT (SELECT COUNT(*) FROM kpi_sync_outbox WHERE guild_id = ?) + "
            "(SELECT COUNT(*) FROM audit_kpi_sync_outbox WHERE guild_id = ?) + "
            "(SELECT COUNT(*) FROM sanction_sync_outbox WHERE guild_id = ?)",
            (guild_id, guild_id, guild_id),
        )).fetchone())[0])
    return {
        "pending_over_24h": pending_24h,
        "pending_over_72h": pending_72h,
        "active_learning_rules": active_learning,
        "ai_retry_queue": retry_queue,
        "kpi_sync_pending": kpi_outbox,
    }


async def record_audit_run_metrics(guild_id: int, backend: str,
                                   reviewed_messages: int, flagged_messages: int,
                                   failed_channels: int, target_channels: int,
                                   report_path: str | None = None,
                                   created_at: float | None = None) -> int:
    """배치 감사 실행 결과를 원문 없이 운영 KPI로 저장한다."""
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO audit_run_metrics
               (guild_id, backend, reviewed_messages, flagged_messages, failed_channels,
                target_channels, report_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, backend, max(0, reviewed_messages), max(0, flagged_messages),
             max(0, failed_channels), max(0, target_channels), report_path,
             time.time() if created_at is None else created_at),
        )
        if cursor.rowcount == 1:
            await db.execute(
                """INSERT OR IGNORE INTO audit_kpi_sync_outbox
                   (audit_run_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
                   VALUES (?, ?, 0, 0, NULL, ?)""",
                (int(cursor.lastrowid), guild_id, time.time()),
            )
        await db.commit()
        return int(cursor.lastrowid or 0) if cursor.rowcount == 1 else 0


async def backfill_audit_kpi_sync_outbox(guild_id: int) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO audit_kpi_sync_outbox
               (audit_run_id, guild_id, attempts, next_attempt_at, last_error, updated_at)
               SELECT id, guild_id, 0, 0, NULL, ? FROM audit_run_metrics
               WHERE guild_id = ?""",
            (time.time(), guild_id),
        )
        await db.commit()
        return max(0, cursor.rowcount)


async def get_due_audit_kpi_sync_records(limit: int = 25) -> list[dict]:
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT a.id, a.guild_id, a.backend, a.reviewed_messages,
                      a.flagged_messages, a.failed_channels, a.target_channels,
                      a.created_at, o.attempts
               FROM audit_kpi_sync_outbox o
               JOIN audit_run_metrics a
                 ON a.id = o.audit_run_id AND a.guild_id = o.guild_id
               WHERE o.next_attempt_at <= ?
               ORDER BY o.next_attempt_at, o.audit_run_id LIMIT ?""",
            (time.time(), max(1, limit)),
        )
        columns = (
            "audit_run_id", "guild_id", "backend", "reviewed_messages",
            "flagged_messages", "failed_channels", "target_channels", "created_at",
            "sync_attempts",
        )
        return [dict(zip(columns, row)) for row in await cursor.fetchall()]


async def mark_audit_kpi_sync_complete(audit_run_ids: list[int]) -> int:
    if not audit_run_ids:
        return 0
    placeholders = ",".join("?" for _ in audit_run_ids)
    async with _connect() as db:
        cursor = await db.execute(
            f"DELETE FROM audit_kpi_sync_outbox WHERE audit_run_id IN ({placeholders})",
            tuple(map(int, audit_run_ids)),
        )
        await db.commit()
        return cursor.rowcount


async def reschedule_audit_kpi_sync(audit_run_ids: list[int], error_category: str,
                                    delay_seconds: float) -> None:
    if not audit_run_ids:
        return
    next_attempt = time.time() + max(1.0, delay_seconds)
    async with _connect() as db:
        await db.executemany(
            """UPDATE audit_kpi_sync_outbox
               SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?, updated_at = ?
               WHERE audit_run_id = ?""",
            [(next_attempt, str(error_category)[:100], time.time(), int(run_id))
             for run_id in audit_run_ids],
        )
        await db.commit()


async def get_audit_metrics(guild_id: int, start_at: float, end_at: float) -> dict:
    async with _connect() as db:
        row = await (await db.execute(
            """SELECT COUNT(*), COALESCE(SUM(reviewed_messages), 0),
                      COALESCE(SUM(flagged_messages), 0),
                      COALESCE(SUM(failed_channels), 0),
                      COALESCE(SUM(target_channels), 0)
               FROM audit_run_metrics
               WHERE guild_id = ? AND created_at >= ? AND created_at < ?""",
            (guild_id, start_at, end_at),
        )).fetchone()
    runs, reviewed, flagged, failed, targets = map(int, row)
    return {
        "runs": runs,
        "reviewed_messages": reviewed,
        "flagged_messages": flagged,
        "flag_rate_percent": round(flagged / reviewed * 100, 1) if reviewed else None,
        "failed_channels": failed,
        "target_channels": targets,
        "successful_channel_percent": (
            round((targets - failed) / targets * 100, 1) if targets else None
        ),
    }


async def upsert_kpi_period_snapshot(guild_id: int, period_type: str, period_key: str,
                                     period_start: float, period_end: float,
                                     summary_json: str, source_rows: int,
                                     finalized: bool) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO kpi_period_snapshots
               (guild_id, period_type, period_key, period_start, period_end, summary_json,
                source_rows, dirty, finalized, refreshed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
               ON CONFLICT(guild_id, period_type, period_key) DO UPDATE SET
                   period_start = excluded.period_start,
                   period_end = excluded.period_end,
                   summary_json = excluded.summary_json,
                   source_rows = excluded.source_rows,
                   dirty = 0,
                   finalized = excluded.finalized,
                   refreshed_at = excluded.refreshed_at""",
            (guild_id, period_type, period_key, period_start, period_end, summary_json,
             max(0, source_rows), int(finalized), time.time()),
        )
        await db.commit()


async def get_kpi_period_snapshot(guild_id: int, period_type: str,
                                  period_key: str) -> dict | None:
    async with _connect() as db:
        row = await (await db.execute(
            """SELECT period_start, period_end, summary_json, source_rows, dirty,
                      finalized, refreshed_at
               FROM kpi_period_snapshots
               WHERE guild_id = ? AND period_type = ? AND period_key = ?""",
            (guild_id, period_type, period_key),
        )).fetchone()
    if row is None:
        return None
    return {
        "period_start": row[0], "period_end": row[1], "summary_json": row[2],
        "source_rows": int(row[3]), "dirty": bool(row[4]),
        "finalized": bool(row[5]), "refreshed_at": row[6],
    }


async def mark_kpi_snapshots_dirty_for_timestamp(guild_id: int,
                                                  detected_at: float) -> int:
    """뒤늦은 관리자 확정이 포함되는 기존 월·분기·연 스냅샷만 무효화한다."""
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE kpi_period_snapshots SET dirty = 1
               WHERE guild_id = ? AND period_start <= ? AND period_end > ?""",
            (guild_id, detected_at, detected_at),
        )
        await db.commit()
        return cursor.rowcount


async def prune_kpi_sync_outbox(max_completed_age_days: int = 30) -> int:
    """이미 삭제된 카드·감사 레코드를 가리키는 고아 outbox만 정리한다."""
    cutoff = time.time() - max(0, max_completed_age_days) * 86400
    async with _connect() as db:
        review_cursor = await db.execute(
            """DELETE FROM kpi_sync_outbox
               WHERE updated_at < ? AND NOT EXISTS (
                   SELECT 1 FROM violation_log v
                   WHERE v.id = kpi_sync_outbox.review_id
                     AND v.guild_id = kpi_sync_outbox.guild_id
               )""",
            (cutoff,),
        )
        audit_cursor = await db.execute(
            """DELETE FROM audit_kpi_sync_outbox
               WHERE updated_at < ? AND NOT EXISTS (
                   SELECT 1 FROM audit_run_metrics a
                   WHERE a.id = audit_kpi_sync_outbox.audit_run_id
                     AND a.guild_id = audit_kpi_sync_outbox.guild_id
               )""",
            (cutoff,),
        )
        sanction_cursor = await db.execute(
            """DELETE FROM sanction_sync_outbox
               WHERE updated_at < ? AND NOT EXISTS (
                   SELECT 1 FROM sanction_records s
                   WHERE s.id = sanction_sync_outbox.sanction_id
                     AND s.guild_id = sanction_sync_outbox.guild_id
               )""",
            (cutoff,),
        )
        await db.execute("PRAGMA optimize")
        await db.commit()
        return review_cursor.rowcount + audit_cursor.rowcount + sanction_cursor.rowcount


async def kpi_report_was_delivered(guild_id: int, period_type: str,
                                   period_key: str) -> bool:
    async with _connect() as db:
        row = await (await db.execute(
            "SELECT 1 FROM kpi_report_deliveries "
            "WHERE guild_id = ? AND period_type = ? AND period_key = ?",
            (guild_id, period_type, period_key),
        )).fetchone()
        return row is not None


async def mark_kpi_report_delivered(guild_id: int, period_type: str, period_key: str,
                                    channel_id: int | None, message_id: int | None) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO kpi_report_deliveries
               (guild_id, period_type, period_key, channel_id, message_id, delivered_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, period_type, period_key) DO NOTHING""",
            (guild_id, period_type, period_key, channel_id, message_id, time.time()),
        )
        await db.commit()


async def create_review_record(
    guild_id, user_id, channel_id, message_id, message_content,
    level, reason, action_taken, provider="unknown", card_delivered=True,
    rule_violated="-", detection_source="realtime",
    channel_name=None, channel_group=None, learning_scope_channel_id=None,
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
        superseded_ids = []
        if message_id is not None:
            cursor = await db.execute(
                "SELECT id FROM violation_log WHERE guild_id = ? AND message_id = ? "
                "AND review_status IN ('pending', 'processing')",
                (guild_id, message_id),
            )
            superseded_ids = [int(row[0]) for row in await cursor.fetchall()]
            await db.execute(
                "UPDATE violation_log SET review_status = 'superseded' "
                "WHERE guild_id = ? AND message_id = ? "
                "AND review_status IN ('pending', 'processing')",
                (guild_id, message_id),
            )
        cursor = await db.execute(
            """INSERT INTO violation_log
               (guild_id, user_id, channel_id, message_id, message_content, level, reason,
                action_taken, provider, needs_review, review_status, card_delivered,
                language_group, rule_violated, detection_source, channel_name, channel_group,
                learning_scope_channel_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, user_id, channel_id, message_id, message_content, level, reason,
             action_taken, provider, int(card_delivered),
             detect_language_group(message_content), str(rule_violated)[:100],
             str(detection_source)[:30], str(channel_name)[:100] if channel_name else None,
             str(channel_group)[:100] if channel_group else None,
             int(learning_scope_channel_id) if learning_scope_channel_id else None,
             time.time()),
        )
        review_id = int(cursor.lastrowid)
        for superseded_id in superseded_ids:
            await _enqueue_kpi_sync(db, superseded_id, guild_id)
        await _enqueue_kpi_sync(db, review_id, guild_id)
        await db.commit()
        return review_id


async def mark_review_delivery_failed(review_id: int, guild_id: int):
    """검수 카드 전송이 실패했음을 기록한다 (pending은 유지, 카드만 없음 표시)."""
    async with _connect() as db:
        updated = await db.execute(
            "UPDATE violation_log SET card_delivered = 0 "
            "WHERE id = ? AND guild_id = ? AND review_status = 'pending'",
            (review_id, guild_id),
        )
        if updated.rowcount == 1:
            await _enqueue_kpi_sync(db, review_id, guild_id)
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
    sanction: dict | None = None,
):
    if status not in {"confirmed", "false_positive"}:
        raise ValueError(f"알 수 없는 검수 상태: {status}")
    async with _connect() as db:
        updated = await db.execute(
            """UPDATE violation_log
               SET review_status = ?, reviewed_at = ?, reviewed_by = ?,
                   action_taken = ?, needs_review = 0
               WHERE id = ? AND guild_id = ? AND review_status = 'processing'""",
            (status, time.time(), reviewer_id, action_taken, review_id, guild_id),
        )
        if updated.rowcount == 1:
            verdict = "violation" if status == "confirmed" else "normal"
            corrected_level = "NONE" if status == "false_positive" else None
            await db.execute(
                """INSERT INTO moderation_labels
                   (review_id, guild_id, verdict, corrected_level, marked_by, created_at)
                   SELECT id, guild_id, ?, COALESCE(?, level), ?, ?
                   FROM violation_log WHERE id = ? AND guild_id = ?
                   ON CONFLICT(review_id) DO UPDATE SET
                       verdict = excluded.verdict,
                       corrected_level = excluded.corrected_level,
                       marked_by = excluded.marked_by,
                       created_at = excluded.created_at""",
                (verdict, corrected_level, reviewer_id, time.time(), review_id, guild_id),
            )
            await _enqueue_kpi_sync(db, review_id, guild_id)
            await _mark_kpi_snapshots_dirty_for_review(db, review_id, guild_id)
            if sanction is not None:
                now = time.time()
                dedupe_key = str(sanction["dedupe_key"])[:160]
                inserted = await db.execute(
                    """INSERT OR IGNORE INTO sanction_records
                       (guild_id, user_id, user_display, action_type, reason, source,
                        status, issued_at, expires_at, issued_by_id, issued_by_display,
                        review_id, dedupe_key, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(guild_id), int(sanction["user_id"]),
                        str(sanction["user_display"])[:120],
                        str(sanction["action_type"])[:32],
                        str(sanction["reason"])[:1000], str(sanction["source"])[:32],
                        float(sanction.get("issued_at", now)), sanction.get("expires_at"),
                        reviewer_id, str(sanction.get("issued_by_display") or "")[:120],
                        review_id, dedupe_key, now, now,
                    ),
                )
                row = await (await db.execute(
                    "SELECT id FROM sanction_records WHERE guild_id = ? AND dedupe_key = ?",
                    (int(guild_id), dedupe_key),
                )).fetchone()
                if inserted.rowcount == 1 and row:
                    await db.execute(
                        """INSERT OR REPLACE INTO sanction_sync_outbox
                           (sanction_id, guild_id, attempts, next_attempt_at,
                            last_error, updated_at)
                           VALUES (?, ?, 0, 0, NULL, ?)""",
                        (int(row[0]), int(guild_id), now),
                    )
        await db.commit()
        return updated.rowcount == 1


async def get_moderation_training_examples(guild_id: int, limit: int = 100):
    """Return administrator-confirmed examples whose retained message text still exists."""
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT v.message_content, l.verdict, l.corrected_level,
                      v.reason, v.provider, l.created_at
               FROM moderation_labels AS l
               JOIN violation_log AS v ON v.id = l.review_id AND v.guild_id = l.guild_id
               WHERE l.guild_id = ? AND v.message_content IS NOT NULL
                 AND TRIM(v.message_content) != ''
               ORDER BY l.created_at DESC LIMIT ?""",
            (guild_id, max(1, limit)),
        )
        return await cursor.fetchall()


async def get_moderation_label_stats(guild_id: int):
    """Return confirmed normal/violation counts grouped by offline language family."""
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT v.language_group, l.verdict, COUNT(*)
               FROM moderation_labels AS l
               JOIN violation_log AS v ON v.id = l.review_id AND v.guild_id = l.guild_id
               WHERE l.guild_id = ?
               GROUP BY v.language_group, l.verdict
               ORDER BY COUNT(*) DESC, v.language_group""",
            (guild_id,),
        )
        return await cursor.fetchall()


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


async def get_review_learning_scope(review_id: int, guild_id: int) -> int | None:
    """감지 당시 저장한 오탐 학습 범위를 반환한다. 이전 스키마 행은 None이다."""
    async with _connect() as db:
        cursor = await db.execute(
            "SELECT learning_scope_channel_id FROM violation_log "
            "WHERE id = ? AND guild_id = ?",
            (review_id, guild_id),
        )
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] else None


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
                                     marked_by: int | None,
                                     learning_explanation: str | None = None) -> int:
    now = time.time()
    async with _connect() as db:
        await db.execute(
            """INSERT INTO false_positive_rules
               (guild_id, scope_channel_id, content_hash, content, wrong_level, wrong_reason,
                source_channel_id, marked_by, active, created_at, updated_at,
                learning_explanation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(guild_id, scope_channel_id, content_hash) DO UPDATE SET
                   content = excluded.content,
                   wrong_level = excluded.wrong_level,
                   wrong_reason = excluded.wrong_reason,
                   source_channel_id = excluded.source_channel_id,
                   marked_by = excluded.marked_by,
                   learning_explanation = excluded.learning_explanation,
                   active = 1,
                   updated_at = excluded.updated_at""",
            (guild_id, scope_channel_id, content_hash, content, wrong_level, wrong_reason,
             source_channel_id, marked_by, now, now, learning_explanation),
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
                                           reviewer_id: int, action_taken: str,
                                           learning_explanation: str | None = None):
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
                source_channel_id, marked_by, active, created_at, updated_at,
                learning_explanation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(guild_id, scope_channel_id, content_hash) DO UPDATE SET
                   content = excluded.content,
                   wrong_level = excluded.wrong_level,
                   wrong_reason = excluded.wrong_reason,
                   source_channel_id = excluded.source_channel_id,
                   marked_by = excluded.marked_by,
                   learning_explanation = excluded.learning_explanation,
                   active = 1,
                   updated_at = excluded.updated_at""",
            (guild_id, scope_channel_id, content_hash, stored_content, wrong_level, wrong_reason,
             source_channel_id, reviewer_id, now, now, learning_explanation),
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
                   action_taken = ?, review_note = ?, needs_review = 0
               WHERE id = ? AND guild_id = ? AND review_status = 'processing'""",
            (now, reviewer_id, action_taken, learning_explanation, review_id, guild_id),
        )
        if updated.rowcount != 1:
            await db.rollback()
            return None
        await db.execute(
            """INSERT INTO moderation_labels
               (review_id, guild_id, verdict, corrected_level, marked_by, created_at)
               VALUES (?, ?, 'normal', 'NONE', ?, ?)
               ON CONFLICT(review_id) DO UPDATE SET
                   verdict = excluded.verdict,
                   corrected_level = excluded.corrected_level,
                   marked_by = excluded.marked_by,
                   created_at = excluded.created_at""",
            (review_id, guild_id, reviewer_id, now),
        )
        await _enqueue_kpi_sync(db, review_id, guild_id, now)
        await _mark_kpi_snapshots_dirty_for_review(db, review_id, guild_id)
        await db.commit()
        return {"id": rule_id, "content": content}


async def get_false_positive_thread_scope_candidates():
    """부모 확인이 필요한, 출처 채널 자체에 묶인 활성 규칙 범위를 반환한다."""
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT DISTINCT guild_id, source_channel_id, scope_channel_id
               FROM false_positive_rules
               WHERE active = 1 AND scope_channel_id != 0
                 AND source_channel_id IS NOT NULL
                 AND scope_channel_id = source_channel_id"""
        )
        return await cursor.fetchall()


async def migrate_false_positive_thread_scope(guild_id: int, source_channel_id: int,
                                                old_scope_id: int,
                                                parent_scope_id: int) -> int:
    """개별 스레드 규칙을 부모 채널 범위로 병합하고 이전 행은 이력으로 보존한다."""
    if old_scope_id == parent_scope_id:
        return 0
    async with _connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """SELECT id, content_hash, content, wrong_level, wrong_reason, marked_by,
                      created_at, updated_at, learning_explanation
               FROM false_positive_rules
               WHERE guild_id = ? AND source_channel_id = ?
                 AND scope_channel_id = ? AND active = 1""",
            (guild_id, source_channel_id, old_scope_id),
        )
        rows = await cursor.fetchall()
        if not rows:
            await db.rollback()
            return 0

        now = time.time()
        for (_, stored_hash, content, wrong_level, wrong_reason, marked_by,
             created_at, updated_at, learning_explanation) in rows:
            await db.execute(
                """INSERT INTO false_positive_rules
                   (guild_id, scope_channel_id, content_hash, content, wrong_level,
                    wrong_reason, source_channel_id, marked_by, active, created_at, updated_at,
                    learning_explanation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(guild_id, scope_channel_id, content_hash) DO UPDATE SET
                       active = 1,
                       learning_explanation = COALESCE(
                           excluded.learning_explanation,
                           false_positive_rules.learning_explanation
                       ),
                       updated_at = MAX(false_positive_rules.updated_at, excluded.updated_at)""",
                (guild_id, parent_scope_id, stored_hash, content, wrong_level, wrong_reason,
                 source_channel_id, marked_by, created_at, updated_at, learning_explanation),
            )

        row_ids = [int(row[0]) for row in rows]
        placeholders = ",".join("?" for _ in row_ids)
        await db.execute(
            f"UPDATE false_positive_rules SET active = 0, updated_at = ? "
            f"WHERE id IN ({placeholders})",
            (now, *row_ids),
        )
        await db.execute(
            """UPDATE violation_log SET learning_scope_channel_id = ?
               WHERE guild_id = ? AND channel_id = ?
                 AND (learning_scope_channel_id IS NULL OR learning_scope_channel_id = ?)""",
            (parent_scope_id, guild_id, source_channel_id, old_scope_id),
        )
        await db.commit()
        return len(rows)


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
            """SELECT id, scope_channel_id, content, wrong_level, updated_at,
                      learning_explanation
               FROM false_positive_rules
               WHERE guild_id = ? AND active = 1
                 AND scope_channel_id IN (0, ?)
               ORDER BY CASE WHEN scope_channel_id = ? THEN 0 ELSE 1 END,
                        updated_at DESC LIMIT ?""",
            (guild_id, scope_channel_id, scope_channel_id, limit),
        )
        return await cursor.fetchall()


async def list_false_positive_rules(guild_id: int, limit: int = 20):
    async with _connect() as db:
        cursor = await db.execute(
            """SELECT id, scope_channel_id, content, wrong_level, marked_by, updated_at,
                      learning_explanation
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
