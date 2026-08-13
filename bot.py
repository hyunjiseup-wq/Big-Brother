"""
디스코드 자동 제재 봇 메인 파일 (대규모 서버 + Gemini/Groq/로컬 Ollama 삼중화 버전).

흐름:
1. 메시지 수신 -> filters.fast_check()로 1차 필터링 (정규식/금칙어/스팸, AI 호출 없음)
   - SKIP    : 정상 메시지, 즉시 종료
   - DECIDED : 필터만으로 등급 확정, AI 호출 없이 바로 제재 처리
   - NEEDS_AI: 애매한 경우만 큐에 넣어 워커가 비동기로 AI 판단
2. AI 판단 전, cache에서 동일/반복 문구의 기존 판단 결과가 있는지 먼저 확인
3. moderator.classify_message()가 설정된 순서(기본 Ollama→Gemini→Groq)로 판단하고,
   모두 실패하면 메시지를 영속 재검사 큐에 보류 (무료 한도 소진 시 영구 무감시 방지)
4. 위반 등급에 따라 점수 부여 (config.VIOLATION_LEVEL_POINTS)
5. 누적 점수 -> config.STRIKE_THRESHOLDS 에 따라 조치 결정
   단, 커뮤니티 정책상 킥/밴은 판단 주체(필터/Gemini/Groq/Ollama) 무관하게 자동 실행하지 않고
   config.AUTO_ACTION_CEILING(기본 타임아웃)으로 항상 하향되며, 로그 채널에
   "관리자 검토 필요"로 강조 표시됨 (!BB 검토대기 명령어로 목록 확인 가능)
6. 조치 실행 (경고/삭제/타임아웃/킥/밴) + 로그 채널 기록 (판단 주체 포함)

대규모 트래픽 대응 포인트:
- 모든 메시지를 AI에 보내지 않고 1차 필터로 대부분을 무료로 처리
- asyncio.Queue + 워커 풀로 AI 호출을 config.MAX_CONCURRENT_AI_CALLS개로 제한
  (API 레이트리밋/비용 폭주 방지, 이벤트 루프 블로킹 방지)
- 큐가 가득 차면(트래픽 폭주) 메시지를 버리고 로그만 남겨 봇 다운을 방지
"""
import os
import sys
import asyncio
import time
import json
import re
import unicodedata
from collections import defaultdict, deque
import discord
import httpx
from discord.ext import commands, tasks
from dotenv import load_dotenv
import datetime

import config
import database
import cache
import learning
import kpi
import vision
from filters import FilterResult, extract_discord_invite_urls, fast_check
import moderator
import ollama_runtime
import runtime_lock
from moderator import classify_message, get_channel_note
from batch_audit import backfill_audit_metrics_from_reports, prune_expired_reports, run_full_audit

# Windows 콘솔(cp949)은 이모지를 출력하지 못해 UnicodeEncodeError로
# 이벤트 핸들러가 중단될 수 있으므로 표준 출력을 UTF-8로 강제한다.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

load_dotenv()
config.validate_config()


def _env_int(name: str):
    """환경변수를 int로 안전하게 파싱. 비어있거나 숫자가 아니면 None (int('') 크래시 방지)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"⚠️ 환경변수 {name} 값이 숫자가 아닙니다: {raw!r} (무시함)")
        return None


TOKEN = os.environ["DISCORD_BOT_TOKEN"]
LOG_CHANNEL_ID = _env_int("LOG_CHANNEL_ID")
PUBLIC_LOG_CHANNEL_ID = _env_int("PUBLIC_LOG_CHANNEL_ID")
KPI_REPORT_CHANNEL_ID = (
    _env_int("KPI_REPORT_CHANNEL_ID") or _env_int("REPORT_CHANNEL_ID") or LOG_CHANNEL_ID
)
KPI_DASHBOARD_PUBLIC_URL = os.environ.get("KPI_DASHBOARD_PUBLIC_URL", "").strip()
KPI_DASHBOARD_INGEST_URL = os.environ.get("KPI_DASHBOARD_INGEST_URL", "").strip()
KPI_DASHBOARD_INGEST_TOKEN = os.environ.get("KPI_DASHBOARD_INGEST_TOKEN", "").strip()


def validate_runtime_environment() -> None:
    """네트워크 로그인 전에 필수 환경변수의 누락·형식 오류를 한 번에 알린다."""
    errors = []
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    placeholder_markers = ("여기에_", "your_", "replace_me")
    if not token or any(marker in token.casefold() for marker in placeholder_markers):
        errors.append("DISCORD_BOT_TOKEN에 실제 봇 토큰을 설정해야 합니다.")

    if not (os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GROQ_API_KEY", "").strip()):
        errors.append("실시간 AI 판단을 위해 GEMINI_API_KEY 또는 GROQ_API_KEY 중 하나가 필요합니다.")

    raw_log_channel = os.environ.get("LOG_CHANNEL_ID", "").strip()
    if not raw_log_channel:
        errors.append("LOG_CHANNEL_ID를 설정해야 제재·장애·누락 로그를 확인할 수 있습니다.")
    else:
        try:
            if int(raw_log_channel) <= 0:
                raise ValueError
        except ValueError:
            errors.append("LOG_CHANNEL_ID는 양의 정수 Discord 채널 ID여야 합니다.")

    for name in ("PUBLIC_LOG_CHANNEL_ID", "REPORT_CHANNEL_ID", "KPI_REPORT_CHANNEL_ID"):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                if int(raw) <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"{name}는 비워두거나 양의 정수 Discord 채널 ID를 사용해야 합니다.")

    ingest_url = os.environ.get("KPI_DASHBOARD_INGEST_URL", "").strip()
    ingest_token = os.environ.get("KPI_DASHBOARD_INGEST_TOKEN", "").strip()
    public_url = os.environ.get("KPI_DASHBOARD_PUBLIC_URL", "").strip()
    if bool(ingest_url) != bool(ingest_token):
        errors.append("KPI_DASHBOARD_INGEST_URL과 KPI_DASHBOARD_INGEST_TOKEN은 함께 설정해야 합니다.")
    if ingest_url and not ingest_url.startswith("https://"):
        errors.append("KPI_DASHBOARD_INGEST_URL은 HTTPS 주소여야 합니다.")
    if public_url and not public_url.startswith("https://"):
        errors.append("KPI_DASHBOARD_PUBLIC_URL은 HTTPS 주소여야 합니다.")

    if errors:
        raise RuntimeError("환경 설정 오류:\n- " + "\n- ".join(errors))


def permission_warnings(member) -> list[str]:
    """봇 멤버에 부여된 과도하거나 부족한 서버 권한 경고를 반환한다."""
    permissions = getattr(member, "guild_permissions", None)
    if permissions is None:
        return ["봇의 서버 권한을 확인할 수 없습니다."]
    warnings = []
    if permissions.administrator and not config.ALLOW_ADMINISTRATOR_PERMISSION:
        warnings.append(
            "Administrator 권한이 부여돼 있습니다. 탈취·오작동 피해를 줄이려면 제거하고 "
            "메시지 관리, 멤버 타임아웃, 추방, 차단 권한만 부여하세요."
        )
    required = {
        "manage_messages": "메시지 관리",
        "moderate_members": "멤버 타임아웃",
        "kick_members": "멤버 추방",
        "ban_members": "멤버 차단",
        "view_audit_log": "감사 로그 보기",
    }
    missing = [label for attr, label in required.items() if not getattr(permissions, attr, False)]
    if missing:
        warnings.append("필수 권한이 부족합니다: " + ", ".join(missing))
    return warnings

# 등급 서열 (공개 로그 최소 등급 비교용)
_LEVEL_ORDER = {"MINOR": 1, "MODERATE": 2, "SEVERE": 3, "EXTREME": 4}

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True

# 명령어 접두사: Big Brother의 약자 "BB". "!BB 점수"처럼 띄어 써도 "!BB점수"처럼
# 붙여 써도 되고, 대소문자도 구분하지 않는다.
# 주의: 공백 포함 접두사를 앞에 둬야 "!BB 점수"가 '점수' 명령어로 올바르게 파싱된다.
COMMAND_PREFIXES = ("!BB ", "!bb ", "!Bb ", "!bB ", "!BB", "!bb", "!Bb", "!bB")
# 기본 영어 help 명령어는 끄고, 한글 "!BB 명령어"(별칭: 도움말/help)로 대체한다.
bot = commands.Bot(command_prefix=list(COMMAND_PREFIXES), intents=intents, help_command=None)


@bot.check
async def _admin_only_commands(ctx: commands.Context) -> bool:
    """서버 정책: 제재봇의 모든 명령어는 관리자만 사용할 수 있다 (DM에서는 사용 불가)."""
    perms = getattr(ctx.author, "guild_permissions", None)
    return ctx.guild is not None and perms is not None and perms.administrator


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """명령어 오류를 조용한 콘솔 예외 대신 사용자가 이해할 수 있는 안내로 바꾼다."""
    try:
        if isinstance(error, commands.CommandNotFound):
            return  # 접두사와 겹친 일반 대화/오타는 조용히 무시
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            await ctx.send("⛔ 제재봇 명령어는 관리자만 사용할 수 있습니다.")
        elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.send("사용법이 올바르지 않습니다. `!BB 명령어`에서 사용법을 확인해주세요.")
        else:
            print(f"[command] '{ctx.command}' 처리 중 오류: {error}")
    except (discord.Forbidden, discord.HTTPException):
        pass  # 안내 메시지를 보낼 권한이 없으면 무시

# AI 판단이 필요한 메시지를 담아두는 큐 (워커들이 소비)
_message_queue: asyncio.Queue = asyncio.Queue(maxsize=config.MAX_QUEUE_SIZE)
_ai_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_AI_CALLS)
_dropped_count = 0        # 큐가 가득 차서 검사도 못 하고 버린 메시지 (감시 구멍)
_expired_count = 0        # 큐에서 너무 오래 대기해 폐기한 메시지
_last_drop_at: datetime.datetime | None = None
_last_drop_alert_at: datetime.datetime | None = None
_workers_started = False  # on_ready는 재연결 시마다 다시 호출되므로 워커 중복 생성 방지용
_pending_bot_timeout_changes: dict[tuple[int, int], tuple[float, float]] = {}
_pending_bot_bans: dict[tuple[int, int], float] = {}


def _remember_bot_timeout_change(guild_id: int, user_id: int, expires_at: float | None):
    """봇 자체 타임아웃 변경을 멤버 이벤트에서 수동 조치로 중복 기록하지 않게 표시한다."""
    _pending_bot_timeout_changes[(int(guild_id), int(user_id))] = (
        float(expires_at or 0), time.monotonic() + 30,
    )


def _consume_bot_timeout_change(guild_id: int, user_id: int,
                                expires_at: float | None) -> bool:
    key = (int(guild_id), int(user_id))
    expected = _pending_bot_timeout_changes.get(key)
    if expected is None:
        return False
    expected_expires, deadline = expected
    if time.monotonic() > deadline:
        _pending_bot_timeout_changes.pop(key, None)
        return False
    actual = float(expires_at or 0)
    if abs(expected_expires - actual) <= 5:
        _pending_bot_timeout_changes.pop(key, None)
        return True
    return False


def _remember_bot_ban(guild_id: int, user_id: int):
    """봇이 실행한 밴을 on_member_ban에서 Discord 직접 조치로 중복 기록하지 않게 한다."""
    _pending_bot_bans[(int(guild_id), int(user_id))] = time.monotonic() + 30


def _consume_bot_ban(guild_id: int, user_id: int) -> bool:
    key = (int(guild_id), int(user_id))
    deadline = _pending_bot_bans.pop(key, None)
    return deadline is not None and time.monotonic() <= deadline


def _member_ledger_display(member, user_id: int) -> str:
    if member is None:
        return f"Discord 사용자 {user_id}"
    display_name = getattr(member, "display_name", None)
    account = str(member)
    return f"{display_name} ({account})" if display_name and display_name != account else account


def _sanction_actor_text(display: str | None, actor_id: int | None) -> str:
    """제재 조회 화면에서 이름 변경 후에도 식별 가능한 처리자 표기를 만든다."""
    if actor_id is None:
        return display or "처리자 미확인"
    return f"{display or '이름 미확인'} (`{int(actor_id)}`)"


_processing_keys: set[tuple[int, int, str]] = set()
_background_tasks: set[asyncio.Task] = set()
_kpi_http_client: httpx.AsyncClient | None = None
_kpi_backfill_done = False

# 채널별 최근 메시지를 Discord API 재조회 없이 잠깐 보관한다. 분할 발화 판단에만 쓰며
# 작성자 ID는 AI에 전달하지 않고 current_user/other_user 표기로 바꾼다.
_split_message_buffers: dict[tuple[int, int], deque] = defaultdict(
    lambda: deque(maxlen=128)
)
_split_buffer_record_count = 0


def _record_split_message(message: discord.Message) -> None:
    """분할 발화 문맥용으로 채널의 최신 메시지를 중복 없이 기록한다."""
    if not config.SPLIT_MESSAGE_CONTEXT_ENABLED:
        return
    global _split_buffer_record_count
    key = (int(message.guild.id), int(message.channel.id))
    now = time.monotonic()
    entries = _split_message_buffers[key]
    message_id = int(message.id)
    for index, entry in enumerate(entries):
        if entry[0] == message_id:
            entries[index] = (message_id, int(message.author.id), now, message.content)
            break
    else:
        entries.append((message_id, int(message.author.id), now, message.content))

    _split_buffer_record_count += 1
    if _split_buffer_record_count % 1000 == 0:
        stale_before = now - max(300.0, config.SPLIT_MESSAGE_WINDOW_SECONDS * 4)
        stale_keys = [
            buffer_key for buffer_key, buffer in _split_message_buffers.items()
            if not buffer or buffer[-1][2] < stale_before
        ]
        for buffer_key in stale_keys:
            del _split_message_buffers[buffer_key]


async def _split_message_context(message: discord.Message, *, settle: bool = False) -> list[dict]:
    """짧은 시간의 다자 대화를 화자별로 분리해 대상 메시지의 앞뒤 문맥으로 반환한다."""
    if not config.SPLIT_MESSAGE_CONTEXT_ENABLED:
        return []
    key = (int(message.guild.id), int(message.channel.id))
    entries = _split_message_buffers.get(key)
    if not entries:
        return []

    message_id = int(message.id)
    current = next((entry for entry in entries if entry[0] == message_id), None)
    if current is None:
        return []
    if settle:
        remaining = config.SPLIT_MESSAGE_SETTLE_SECONDS - (time.monotonic() - current[2])
        if remaining > 0:
            await asyncio.sleep(remaining)

    snapshot = list(_split_message_buffers.get(key, ()))
    try:
        current_index = next(i for i, entry in enumerate(snapshot) if entry[0] == message_id)
    except StopIteration:
        return []

    current = snapshot[current_index]
    author_id = current[1]
    reference = getattr(message, "reference", None)
    reply_parent_id = getattr(reference, "message_id", None)
    reply_parent_entry = next(
        (entry for entry in snapshot if entry[0] == reply_parent_id), None
    )
    reply_author_id = reply_parent_entry[1] if reply_parent_entry else None
    selected_before = [
        entry for entry in snapshot[:current_index]
        if current[2] - entry[2] <= config.SPLIT_MESSAGE_WINDOW_SECONDS
    ][-(config.SPLIT_MESSAGE_MAX_MESSAGES - 1):]
    remaining_slots = config.SPLIT_MESSAGE_MAX_MESSAGES - 1 - len(selected_before)
    selected_after = [
        entry for entry in snapshot[current_index + 1:]
        if entry[2] - current[2] <= config.SPLIT_MESSAGE_WINDOW_SECONDS
    ][:remaining_slots]

    remaining_chars = config.SPLIT_MESSAGE_MAX_CHARS
    context = []
    other_authors = {}
    for relation, group in (("before", selected_before), ("after", selected_after)):
        for entry_message_id, entry_author_id, _, raw_content in group:
            content = (raw_content or "").strip()
            if not content or remaining_chars <= 0:
                continue
            content = content[:remaining_chars]
            remaining_chars -= len(content)
            if entry_author_id == author_id:
                speaker = "current_user"
            elif reply_author_id is not None and entry_author_id == reply_author_id:
                speaker = "replied_user"
            else:
                speaker = other_authors.setdefault(
                    entry_author_id, f"other_user_{len(other_authors) + 1}"
                )
            context.append({
                "speaker": speaker,
                "relation": "reply_parent" if entry_message_id == reply_parent_id else relation,
                "content": content,
            })
    return context


def _reply_parent_id(message: discord.Message) -> int | None:
    if not config.REPLY_CONTEXT_ENABLED:
        return None
    value = getattr(getattr(message, "reference", None), "message_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _reply_message_context(
        message: discord.Message, existing_context: list[dict] | None = None) -> list[dict]:
    """Discord 답글 원문을 가져와 생략된 주어·대상을 해석할 명시적 문맥으로 만든다."""
    parent_id = _reply_parent_id(message)
    if parent_id is None or any(
            turn.get("relation") == "reply_parent" for turn in (existing_context or [])):
        return []

    reference = getattr(message, "reference", None)
    parent = getattr(reference, "resolved", None)
    if parent is None or getattr(parent, "id", None) != parent_id:
        fetch_message = getattr(message.channel, "fetch_message", None)
        if fetch_message is None:
            return []
        try:
            parent = await fetch_message(parent_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return []

    content = (getattr(parent, "content", "") or "").strip()
    if not content:
        return []
    parent_author_id = getattr(getattr(parent, "author", None), "id", None)
    speaker = "current_user" if parent_author_id == message.author.id else "replied_user"
    return [{
        "speaker": speaker,
        "relation": "reply_parent",
        "content": content[:config.REPLY_CONTEXT_MAX_CHARS],
    }]


def _has_recent_same_author_fragment(message: discord.Message) -> bool:
    """현재 메시지 앞에 같은 작성자의 짧은 간격 메시지가 있어 재구성이 필요한지 확인한다."""
    key = (int(message.guild.id), int(message.channel.id))
    entries = list(_split_message_buffers.get(key, ()))
    current = next((entry for entry in reversed(entries) if entry[0] == int(message.id)), None)
    if current is None:
        return False
    own_recent = [
        entry for entry in entries
        if (entry[1] == current[1]
            and 0 <= current[2] - entry[2] <= config.SPLIT_MESSAGE_WINDOW_SECONDS
            and (entry[3] or "").strip())
    ]
    return len(own_recent) >= 2


def _newer_same_author_is_processing(message: discord.Message) -> bool:
    """같은 발화의 최신 조각이 이미 검사 중이면 이전 조각의 중복 판단을 생략한다."""
    key = (int(message.guild.id), int(message.channel.id))
    entries = list(_split_message_buffers.get(key, ()))
    current = next((entry for entry in entries if entry[0] == int(message.id)), None)
    if current is None:
        return False
    newer_ids = {
        entry[0] for entry in entries
        if (entry[1] == current[1]
            and 0 < entry[2] - current[2] <= config.SPLIT_MESSAGE_WINDOW_SECONDS)
    }
    return any(key_[0] == message.guild.id and key_[1] in newer_ids for key_ in _processing_keys)


async def _conversation_context_for_message(
        message: discord.Message, *, settle: bool, barter_context: bool) -> tuple[list[dict], list[dict]]:
    """화자별 분절 문맥과 답글 원문을 합치되 거래 채널의 장기 문맥은 보존한다."""
    burst_context = await _split_message_context(message, settle=settle)
    reply_context = await _reply_message_context(message, burst_context)
    if barter_context:
        conversation_context = await _barter_conversation_context(message)
        conversation_context.extend(reply_context)
        conversation_context.extend(
            turn for turn in burst_context
            if turn.get("relation") in ("after", "reply_parent")
        )
    else:
        conversation_context = reply_context + burst_context
    return conversation_context, burst_context


def _split_candidate_contains_keyword(message: discord.Message) -> bool:
    """현재 사용자 자신의 최근 조각을 합쳤을 때 금칙어가 만들어지는지 확인한다."""
    key = (int(message.guild.id), int(message.channel.id))
    entries = list(_split_message_buffers.get(key, ()))
    current = next((entry for entry in reversed(entries) if entry[0] == int(message.id)), None)
    if current is None:
        return False
    own_parts = [
        str(entry[3]) for entry in entries
        if (entry[1] == current[1]
            and 0 <= current[2] - entry[2] <= config.SPLIT_MESSAGE_WINDOW_SECONDS)
    ][-config.SPLIT_MESSAGE_MAX_MESSAGES:]
    if len(own_parts) < 2:
        return False
    joined = unicodedata.normalize("NFKC", "".join(own_parts)).casefold()
    return any(
        unicodedata.normalize("NFKC", word).casefold() in joined
        for word in (*config.BANNED_WORDS_SEVERE, *config.BANNED_WORDS_MODERATE)
        if word
    )


def _spawn(coro):
    """백그라운드 태스크를 유지하고 예상치 못한 오류를 즉시 기록한다."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def done(completed: asyncio.Task):
        _background_tasks.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            print(f"[background-task] 처리 중 오류: {error}")

    task.add_done_callback(done)
    return task


def determine_action(total_points: float, level: str):
    """
    누적 점수와 이번 위반 등급을 바탕으로 조치를 결정한다.
    커뮤니티 정책상 킥/밴은 운영진이 최종 결정해야 하므로, 판단 주체(필터/Gemini/Groq)와
    무관하게 KICK/BAN이 나오면 항상 config.AUTO_ACTION_CEILING으로 자동 하향하고
    관리자 검토가 필요함을 표시한다.
    반환값: (action, duration_minutes, downgraded: bool)
    """
    # EXTREME 등급은 즉시 강제 조치 우선 적용
    if level == "EXTREME" and config.IMMEDIATE_ACTION_FOR_EXTREME:
        action, duration = config.IMMEDIATE_ACTION_FOR_EXTREME, config.IMMEDIATE_TIMEOUT_MINUTES
    else:
        action, duration = "NONE", None
        for threshold, act, dur in config.STRIKE_THRESHOLDS:
            if total_points >= threshold:
                action, duration = act, dur

    downgraded = False
    if action in ("KICK", "BAN"):
        action = config.AUTO_ACTION_CEILING
        duration = config.AUTO_ACTION_CEILING_TIMEOUT_MINUTES
        downgraded = True

    return action, duration, downgraded


async def apply_action(message: discord.Message, action: str, duration_minutes, reason: str):
    """결정된 조치를 실행하고 (핵심 조치 성공 여부, 상세)를 반환한다."""
    member = message.author
    guild = message.guild
    details = []
    delete_ok = True
    primary_ok = action in ("NONE",)
    dm_destination = member

    # 킥/밴 뒤에는 공통 서버가 사라져 새 DM 채널 생성이 거부될 수 있다.
    # 조치 전 채널만 확보하고, 실제 메시지는 핵심 조치 성공 뒤에 보낸다.
    if config.USER_SANCTION_DM_ENABLED and action in ("KICK", "BAN"):
        dm_destination = await _prepare_dm_destination(member)

    if action in ("DELETE", "TIMEOUT", "KICK", "BAN"):
        try:
            await message.delete()
            details.append("메시지 삭제 성공")
        except discord.NotFound:
            details.append("메시지 이미 삭제됨")
        except (discord.Forbidden, discord.HTTPException) as e:
            delete_ok = False
            details.append(f"메시지 삭제 실패: {type(e).__name__}")

    if action == "TIMEOUT":
        try:
            until = discord.utils.utcnow() + datetime.timedelta(minutes=duration_minutes)
            _remember_bot_timeout_change(guild.id, member.id, until.timestamp())
            await member.timeout(until, reason=reason)
            primary_ok = True
            details.append("타임아웃 성공")
        except (discord.Forbidden, discord.HTTPException) as e:
            _pending_bot_timeout_changes.pop((int(guild.id), int(member.id)), None)
            details.append(f"타임아웃 실패: {type(e).__name__}")

    elif action == "KICK":
        try:
            await member.kick(reason=reason)
            primary_ok = True
            details.append("킥 성공")
        except (discord.Forbidden, discord.HTTPException) as e:
            details.append(f"킥 실패: {type(e).__name__}")

    elif action == "BAN":
        try:
            _remember_bot_ban(guild.id, member.id)
            await member.ban(reason=reason, delete_message_days=1)
            primary_ok = True
            details.append("밴 성공")
        except (discord.Forbidden, discord.HTTPException) as e:
            _pending_bot_bans.pop((int(guild.id), int(member.id)), None)
            details.append(f"밴 실패: {type(e).__name__}")
    elif action == "DELETE":
        primary_ok = delete_ok

    # 사용자 제재 DM은 운영 설정으로 명시적으로 켠 경우에만 보낸다.
    should_notify = action == "WARN" or primary_ok
    if action != "NONE" and config.USER_SANCTION_DM_ENABLED and should_notify:
        action_text = {
            "WARN": "경고",
            "DELETE": "메시지 삭제 및 경고",
            "TIMEOUT": f"{duration_minutes}분 타임아웃",
            "KICK": "서버에서 추방",
            "BAN": "서버에서 영구 차단",
        }.get(action, action)
        dm_ok = await _dm_member(
            dm_destination, guild.name, action_text, reason,
            guild_id=guild.id,
            channel_id=message.channel.id,
            message_id=message.id,
            message_content=message.content,
            channel_display=getattr(message.channel, "mention", None),
            created_at=getattr(message, "created_at", None),
            jump_url=getattr(message, "jump_url", None),
        )
        if dm_ok:
            details.append("DM 성공")
            if action == "WARN":
                primary_ok = True
        else:
            details.append("DM 실패: 사용자 DM 차단 또는 Discord API 오류")
    elif action != "NONE" and config.USER_SANCTION_DM_ENABLED and not should_notify:
        # 실패한 조치를 성공한 것처럼 알리면 증거 통지 자체의 신뢰성이 무너진다.
        details.append("핵심 조치 실패로 사용자 DM 미전송")
    elif action == "WARN":
        # DM을 끈 상태의 WARN은 사용자 메시지 없이 내부 경고 기록/점수만 남기는 조치다.
        primary_ok = True
        details.append("사용자 경고 DM 비활성화 (내부 기록만 적용)")

    return primary_ok, "; ".join(details) or "실행할 조치 없음"


async def send_log(guild: discord.Guild, embed: discord.Embed, mention: str = None,
                   view: discord.ui.View = None) -> bool:
    """로그 채널에 임베드를 보낸다. 실제로 전송에 성공하면 True를 반환한다.
    검수 카드처럼 '전송 성공 여부'가 중요한 호출은 이 반환값으로 실패를 감지한다."""
    if not LOG_CHANNEL_ID:
        return False
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        print(f"⚠️ 로그 채널(ID: {LOG_CHANNEL_ID})을 찾을 수 없습니다. .env의 LOG_CHANNEL_ID를 확인하세요.")
        return False
    if not hasattr(channel, "send"):
        print(f"⚠️ 로그 채널 #{channel.name}은(는) {type(channel).__name__}이라 메시지를 보낼 수 없습니다. "
              f"LOG_CHANNEL_ID를 일반 텍스트 채널 ID로 바꿔주세요.")
        return False
    try:
        await channel.send(content=mention, embed=embed, view=view)
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"⚠️ 로그 채널 #{channel.name} 전송 실패: {type(e).__name__}")
        return False


async def send_public_sanction_log(guild: discord.Guild, level: str, rule_violated: str, action: str):
    """
    익명화된 제재 로그를 공개 채널에 게시한다 (운영 투명성 목적).
    커뮤니티 공지 방침에 따라 특정 유저를 식별할 수 있는 정보(멘션, 닉네임, ID, 원문)는
    절대 포함하지 않는다. AI가 생성한 사유 문장에도 메시지 내용/이름이 인용될 수 있으므로
    공개 로그에는 사용하지 않고, 규정 카테고리 기반의 일반화된 설명만 게시한다.
    """
    if not config.PUBLIC_SANCTION_LOG_ENABLED or not PUBLIC_LOG_CHANNEL_ID:
        return
    if _LEVEL_ORDER.get(level, 0) < _LEVEL_ORDER.get(config.PUBLIC_LOG_MIN_LEVEL, 2):
        return

    channel = guild.get_channel(PUBLIC_LOG_CHANNEL_ID)
    if not channel:
        return
    if not hasattr(channel, "send"):
        print(f"⚠️ 공개 로그 채널 #{channel.name}은(는) {type(channel).__name__}이라 메시지를 보낼 수 없습니다. "
              f"PUBLIC_LOG_CHANNEL_ID를 일반 텍스트 채널 ID로 바꿔주세요.")
        return

    action_text = {
        "WARN": "경고", "DELETE": "메시지 삭제 및 경고",
        "TIMEOUT": "타임아웃", "KICK": "추방", "BAN": "차단",
    }.get(action, action)

    # 규정 번호 -> 일반화된 카테고리 설명 (유저 특정 불가능한 수준으로만)
    rule_summaries = {
        "1": "디스코드 가이드라인/BSG 이용약관 위반",
        "2": "커뮤니티 무단 홍보",
        "3": "채팅 예절 미준수",
        "4": "갈등 유발/괴롭힘 관련 행위",
        "5": "핵(치트) 관련 행위",
        "6": "기타 커뮤니티 약관 위반",
    }
    rule_key = str(rule_violated).strip().rstrip(".")
    rule_text = rule_summaries.get(rule_key, "커뮤니티 약관 위반")

    embed = discord.Embed(
        title="📢 제재 안내",
        description="커뮤니티 운영 기준 안내를 위한 익명 제재 로그입니다. 특정 유저를 지칭하지 않습니다.",
        color=discord.Color.dark_grey(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="위반 등급", value=level, inline=True)
    embed.add_field(name="위반 유형", value=rule_text, inline=True)
    embed.add_field(name="조치", value=action_text, inline=True)
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"⚠️ 공개 제재 로그 전송 실패: {type(e).__name__}")


_PROVIDER_LABEL = {"gemini": "Gemini(1차)", "groq": "Groq(2차 폴백)",
                   "ollama": "Ollama(로컬)", "filter": "키워드 필터", "none": "판단 실패"}


async def _handle_violation_review_only(message: discord.Message, level: str, reason_text: str,
                                        rule_violated: str, provider: str, points_to_add: float,
                                        visual_context: dict | None = None):
    """
    수동 검수 모드: 아무 조치도 하지 않고, "자동 모드였다면 어떤 조치가 나갔을지"를
    로그 채널에 올려 관리자가 봇의 판단 정확도를 검증할 수 있게 한다.
    점수도 실제로 부여하지 않는다 (오탐이 점수 기록을 오염시키지 않도록).
    """
    current_points = await database.get_points(message.guild.id, message.author.id)
    would_be_points = current_points + points_to_add
    action, duration, downgraded = determine_action(would_be_points, level)
    action_label = f"{action} {duration}분" if action == "TIMEOUT" and duration else action
    if downgraded:
        action_label += " (킥/밴 후보 → 정책상 타임아웃)"

    review_id = await database.create_review_record(
        message.guild.id, message.author.id, message.channel.id, message.id,
        vision.learning_evidence(message.content, visual_context), level, reason_text,
        f"검수모드(조치 없음, 모의: {action_label})", provider,
        rule_violated=rule_violated, detection_source="realtime",
        channel_name=getattr(message.channel, "name", None),
        channel_group=(
            getattr(getattr(message.channel, "parent", None), "name", None)
            if isinstance(message.channel, discord.Thread)
            else getattr(message.channel, "name", None)
        ),
        learning_scope_channel_id=learning.channel_scope_id(message.channel),
    )

    embed = discord.Embed(
        title="🔍 위반 감지 — 수동 검수 모드 (조치 없음)",
        description="검수 모드라 봇이 아무 조치도 하지 않았습니다. 내용을 확인하고 아래 버튼으로 조치를 선택하세요.",
        color=discord.Color.blue(),
    )
    embed.add_field(name="유저", value=f"{message.author.mention} ({message.author.id})", inline=False)
    embed.add_field(name="채널", value=message.channel.mention, inline=True)
    embed.add_field(name="위반 등급", value=level, inline=True)
    embed.add_field(name="위반 규정", value=str(rule_violated), inline=True)
    embed.add_field(name="판단 주체", value=_PROVIDER_LABEL.get(provider, provider), inline=True)
    embed.add_field(
        name="작성 시각",
        value=f"{discord.utils.format_dt(message.created_at, 'F')} ({discord.utils.format_dt(message.created_at, 'R')})",
        inline=True,
    )
    embed.add_field(name="자동 모드였다면", value=f"{action_label} (점수 {would_be_points:.1f})", inline=False)
    embed.add_field(name="사유", value=reason_text or "-", inline=False)
    embed.add_field(name="원문", value=(message.content[:500] or "(내용 없음)"), inline=False)
    visual_summary = vision.display_summary(visual_context)
    if visual_summary:
        embed.add_field(name="첨부 이미지 OCR·비전", value=visual_summary, inline=False)
    embed.add_field(name="메시지 바로가기", value=message.jump_url, inline=False)
    view = _build_review_view(message.channel.id, message.id, message.author.id, review_id)
    delivered = await send_log(message.guild, embed, view=view)
    if not delivered:
        # 카드가 안 올라가면 관리자는 검수할 방법이 없다. 카드 없음 상태로 표시해
        # `!BB 검토대기`에서 별도로 확인하고 로그 채널 권한을 점검하게 한다.
        await database.mark_review_delivery_failed(review_id, message.guild.id)
        print("⚠️ 검수 카드 전송 실패 — 로그 채널 권한/설정을 확인하세요. "
              "감지 기록은 DB에 남아 `!BB 검토대기`에서 조회됩니다.")

    if config.MANUAL_REVIEW_USER_NOTICE_ENABLED:
        await _send_manual_review_test_notice(message.author, message.guild.name)


async def post_batch_review_card(message: discord.Message, level: str, reason_text: str,
                                 rule_violated: str, provider: str, review_id: int,
                                 visual_context: dict | None = None):
    """
    배치 감사에서 위반 의심으로 걸린 '과거' 메시지를 제재 로그 채널에 검토 카드로 올린다.
    실시간 검수 카드와 동일한 버튼을 달아, 관리자가 링크로 원문을 확인하고 바로 조치할 수 있게 한다.
    (배치 감사는 자동 조치를 하지 않으므로 검수 모드/자동 모드와 무관하게 항상 '카드만' 올린다.)

    검수 레코드(review_id)는 batch_audit._post_review_cards가 카드 게시 여부와 무관하게
    미리 만들어 넘겨준다. 여기서는 카드 게시와 전송 실패 표시만 담당한다.
    """
    points = config.VIOLATION_LEVEL_POINTS.get(level, 0)
    current_points = await database.get_points(message.guild.id, message.author.id)
    would_be_points = current_points + points
    action, duration, downgraded = determine_action(would_be_points, level)
    action_label = f"{action} {duration}분" if action == "TIMEOUT" and duration else action
    if downgraded:
        action_label += " (킥/밴 후보 → 정책상 타임아웃)"

    embed = discord.Embed(
        title="🗂️ 배치 감사 — 위반 의심 (조치 없음)",
        description="정기 배치 감사에서 걸린 과거 메시지입니다. 원문을 확인하고 아래 버튼으로 조치를 선택하세요.",
        color=discord.Color.blue(),
    )
    embed.add_field(name="유저", value=f"{message.author.mention} ({message.author.id})", inline=False)
    embed.add_field(name="채널", value=message.channel.mention, inline=True)
    embed.add_field(name="위반 등급", value=level, inline=True)
    embed.add_field(name="위반 규정", value=str(rule_violated), inline=True)
    embed.add_field(name="판단 주체", value=_PROVIDER_LABEL.get(provider, provider), inline=True)
    embed.add_field(
        name="작성 시각",
        value=f"{discord.utils.format_dt(message.created_at, 'F')} ({discord.utils.format_dt(message.created_at, 'R')})",
        inline=True,
    )
    embed.add_field(name="자동 모드였다면", value=f"{action_label} (점수 {would_be_points:.1f})", inline=False)
    embed.add_field(name="사유", value=reason_text or "-", inline=False)
    embed.add_field(name="원문", value=(message.content[:500] or "(내용 없음)"), inline=False)
    visual_summary = vision.display_summary(visual_context)
    if visual_summary:
        embed.add_field(name="첨부 이미지 OCR·비전", value=visual_summary, inline=False)
    embed.add_field(name="메시지 바로가기", value=message.jump_url, inline=False)
    view = _build_review_view(message.channel.id, message.id, message.author.id, review_id)
    delivered = await send_log(message.guild, embed, view=view)
    if not delivered:
        await database.mark_review_delivery_failed(review_id, message.guild.id)


def _batch_review_callback():
    """배치 감사에 넘길 콜백 (설정이 켜져 있을 때만). run_full_audit이 위반 건마다 호출한다."""
    return post_batch_review_card if config.BATCH_POST_REVIEW_CARDS else None


# ── 검수 버튼 (수동 검수 모드 전용) ─────────────────────────────────
# 버튼의 모든 문맥(채널/메시지/유저 ID)은 custom_id에, 등급/사유는 임베드 자체에 들어있다.
# 덕분에 봇을 재시작해도 예전 검수 카드의 버튼이 계속 동작한다 (메모리에 상태 저장 안 함).

_REVIEW_BTN_PREFIX = "amr:"

_REVIEW_ACTION_LABEL = {
    "ok": "정상 처리 · 이 채널에서 오탐 학습",
    "okg": "정상 처리 · 서버 전체 오탐 학습",
    "del": "메시지 삭제",
    "warn": "경고 기록 (사용자 메시지 없음)",
    "to1": "타임아웃 1시간",
    "to24": "타임아웃 24시간",
    "kick": "킥 (추방)",
    "ban": "밴 (영구 차단)",
}


def _build_review_view(channel_id: int, message_id: int, user_id: int,
                       review_id: int = 0) -> discord.ui.View:
    view = discord.ui.View(timeout=None)

    def add(label, action, style, row):
        view.add_item(discord.ui.Button(
            label=label, style=style, row=row,
            custom_id=(f"{_REVIEW_BTN_PREFIX}{action}:{channel_id}:{message_id}:"
                       f"{user_id}:{review_id}"),
        ))

    add("✅ 정상 · 이 채널 학습", "ok", discord.ButtonStyle.success, 0)
    add("🌐 정상 · 서버 전체 학습", "okg", discord.ButtonStyle.success, 0)
    add("🗑️ 메시지 삭제만", "del", discord.ButtonStyle.secondary, 0)
    add("⚠️ 경고 기록", "warn", discord.ButtonStyle.secondary, 0)
    add("⏱️ 타임아웃 1시간", "to1", discord.ButtonStyle.primary, 1)
    add("⏱️ 타임아웃 24시간", "to24", discord.ButtonStyle.primary, 1)
    add("👢 킥", "kick", discord.ButtonStyle.danger, 1)
    add("🔨 밴", "ban", discord.ButtonStyle.danger, 1)
    return view


def _embed_field(embed: discord.Embed, name: str, default: str = "") -> str:
    for f in embed.fields:
        if f.name == name:
            return f.value or default
    return default


def _build_user_sanction_notice(
        guild_name: str, action_text: str, reason_text: str, *,
        guild_id: int = None, channel_id: int = None, message_id: int = None,
        message_content: str = "",
        channel_display: str = None, created_at=None, jump_url: str = None) -> str:
    """관리자 확정 제재 DM에 사실관계 확인용 메시지 증거를 포함한다."""
    guild_name = (guild_name or "서버")[:100]
    action_text = (action_text or "운영 조치")[:100]
    reason_text = (reason_text or "서버 운영 정책 위반")[:450]

    if created_at is None and message_id:
        try:
            created_at = discord.utils.snowflake_time(int(message_id))
        except (TypeError, ValueError, OverflowError):
            created_at = None
    if created_at is not None:
        timestamp = int(created_at.timestamp())
        time_text = f"<t:{timestamp}:F> (<t:{timestamp}:R>)"
    else:
        time_text = "확인 불가"

    channel_text = (channel_display or "").strip()
    if not channel_text:
        channel_text = f"채널 ID `{channel_id}`" if channel_id else "확인 불가"
    elif channel_id:
        channel_text = f"{channel_text} (ID: `{channel_id}`)"

    if not jump_url and guild_id and message_id and channel_id:
        # 삭제된 메시지도 어느 서버/채널의 어떤 메시지였는지 식별할 수 있게 링크를 복원한다.
        # 실제 메시지가 이미 삭제됐다면 Discord에서 링크가 열리지 않을 수 있다.
        jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    link_text = jump_url or "확인 불가"

    content = (message_content or "").strip()
    content = content[:700] if content else "(텍스트 내용 없음)"
    quoted_content = "\n".join(f"> {line}" for line in content.splitlines())

    notice = (
        f"안녕하세요. **{guild_name}** 서버 운영 정책에 따라 아래 메시지에 대해 조치가 적용되었습니다.\n\n"
        f"- 적용 조치: **{action_text}**\n"
        f"- 판단 사유: {reason_text}\n"
        f"- 작성 채널: {channel_text}\n"
        f"- 작성 시각: {time_text}\n"
        f"- 메시지 바로가기: {link_text}\n\n"
        f"확인된 메시지\n{quoted_content}\n\n"
        "메시지가 이미 삭제된 경우 바로가기가 열리지 않을 수 있습니다. "
        "작성 내용이 본인의 메시지와 다르거나 정황 설명 및 이의 제기가 필요하면 서버 운영진에게 문의해 주세요."
    )
    return notice[:2000]


async def _prepare_dm_destination(member):
    """강제 퇴장 전에 DM 채널을 확보하되 실패하면 기존 Member 전송 경로를 유지한다."""
    create_dm = getattr(member, "create_dm", None)
    if create_dm is None:
        return member
    try:
        return await create_dm()
    except (discord.Forbidden, discord.HTTPException):
        return member


async def _dm_member(
        destination, guild_name: str, action_text: str, reason_text: str, *,
        guild_id: int = None, channel_id: int = None, message_id: int = None,
        message_content: str = "",
        channel_display: str = None, created_at=None, jump_url: str = None):
    if not config.USER_SANCTION_DM_ENABLED:
        return True
    try:
        notice = _build_user_sanction_notice(
            guild_name, action_text, reason_text,
            guild_id=guild_id, channel_id=channel_id, message_id=message_id,
            message_content=message_content, channel_display=channel_display,
            created_at=created_at, jump_url=jump_url,
        )
        await destination.send(notice, allowed_mentions=discord.AllowedMentions.none())
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _send_manual_review_test_notice(member: discord.Member, guild_name: str) -> bool:
    """Optional non-sanction notice for test periods; disabled by default."""
    if not config.MANUAL_REVIEW_USER_NOTICE_ENABLED:
        return False
    try:
        await member.send(config.MANUAL_REVIEW_TEST_NOTICE.format(guild_name=guild_name))
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def _resolve_review_learning_scope(guild: discord.Guild, review_id: int | None,
                                         channel_id: int) -> int:
    """캐시/보관 상태와 무관하게 검수 메시지의 부모 채널 학습 범위를 복원한다."""
    if review_id:
        stored_scope = await database.get_review_learning_scope(review_id, guild.id)
        if stored_scope:
            return stored_scope

    get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
    channel = get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return int(channel_id)
    channel_guild = getattr(channel, "guild", None)
    if channel_guild is not None and int(channel_guild.id) != int(guild.id):
        return int(channel_id)
    return learning.channel_scope_id(channel)


async def _repair_historical_thread_learning_scopes() -> int:
    """과거에 개별 스레드 ID로 저장된 채널 학습을 실제 부모 채널로 병합한다."""
    moved = 0
    candidates = await database.get_false_positive_thread_scope_candidates()
    for guild_id, source_channel_id, old_scope_id in candidates:
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            continue
        get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
        channel = get_channel(int(source_channel_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(source_channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        channel_guild = getattr(channel, "guild", None)
        parent_id = getattr(channel, "parent_id", None)
        if (not parent_id or channel_guild is None
                or int(channel_guild.id) != int(guild_id)):
            continue
        moved += await database.migrate_false_positive_thread_scope(
            int(guild_id), int(source_channel_id), int(old_scope_id), int(parent_id)
        )
    if moved:
        await learning.initialize()
    return moved


async def _apply_review_action(guild: discord.Guild, log_message: discord.Message,
                               admin, action: str, channel_id: int, message_id: int,
                               user_id: int, review_id: int = 0):
    """검수 버튼에 해당하는 실제 조치를 실행한다. 반환: (성공 여부, 결과/오류 설명)"""
    embed = log_message.embeds[0]
    level = _embed_field(embed, "위반 등급", "MODERATE")
    rule = _embed_field(embed, "위반 규정", "-")
    reason_text = _embed_field(embed, "사유", "-")
    original_content = _embed_field(embed, "원문", "")
    reason = f"[{level}] 관리자 검수 확정({admin}) - {reason_text}"[:480]

    member = guild.get_member(user_id)
    if (action in ("to1", "to24", "kick")
            or (action == "warn" and config.USER_SANCTION_DM_ENABLED)) and member is None:
        return False, "대상 유저가 서버에 없어 이 조치를 실행할 수 없습니다."

    if review_id and not await database.claim_review(review_id, guild.id):
        return False, "이미 다른 관리자가 처리했거나 처리 중인 검수 건입니다."

    async def fail(text: str):
        if review_id:
            await database.release_review(review_id, guild.id)
        return False, text

    dm_text = ({
        "del": "메시지 삭제 및 경고", "warn": "경고",
        "to1": "60분 타임아웃", "to24": "24시간 타임아웃",
        "kick": "서버에서 추방", "ban": "서버에서 영구 차단",
    }.get(action) if config.USER_SANCTION_DM_ENABLED else None)
    action_label = (
        "경고 전달" if action == "warn" and config.USER_SANCTION_DM_ENABLED
        else _REVIEW_ACTION_LABEL[action]
    )
    dm_destination = member
    if dm_text and member is not None and action in ("kick", "ban"):
        dm_destination = await _prepare_dm_destination(member)

    get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
    channel = get_channel(channel_id)
    message_for_evidence = None
    evidence_content = original_content
    if dm_text and review_id:
        stored_content = await database.get_violation_content(review_id, guild.id)
        if stored_content:
            evidence_content = stored_content

    # 원문 메시지 삭제 (정상/경고 제외 모든 조치에 포함)
    note = ""
    if action in ("del", "to1", "to24", "kick", "ban") or dm_text:
        try:
            message_for_evidence = (
                await channel.fetch_message(message_id)
                if channel and hasattr(channel, "fetch_message") else None
            )
            if message_for_evidence is not None:
                evidence_content = message_for_evidence.content or evidence_content
                if action in ("del", "to1", "to24", "kick", "ban"):
                    await message_for_evidence.delete()
        except discord.NotFound:
            if action == "del":
                note = " (메시지가 이미 삭제되어 있었음)"
        except (discord.Forbidden, discord.HTTPException):
            if action in ("del", "to1", "to24", "kick", "ban"):
                note = " / 메시지 삭제 실패 (봇 권한 확인)"
                if action == "del":
                    return await fail("봇 권한이 부족해 메시지를 삭제하지 못했습니다.")

    timeout_expires_at = None
    try:
        if action in ("to1", "to24"):
            minutes = 60 if action == "to1" else 1440
            until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
            timeout_expires_at = until.timestamp()
            _remember_bot_timeout_change(guild.id, member.id, timeout_expires_at)
            await member.timeout(until, reason=reason)
        elif action == "kick":
            await member.kick(reason=reason)
        elif action == "ban":
            _remember_bot_ban(guild.id, user_id)
            await guild.ban(member or discord.Object(id=user_id), reason=reason)
    except (discord.Forbidden, discord.HTTPException):
        if member is not None:
            _pending_bot_timeout_changes.pop((int(guild.id), int(member.id)), None)
        _pending_bot_bans.pop((int(guild.id), int(user_id)), None)
        return await fail(
            "봇 권한 또는 Discord API 오류로 조치를 실행하지 못했습니다 "
            "(봇 역할이 대상 유저의 역할보다 위에 있는지 확인)."
        )

    dm_ok = True
    if dm_text and member is not None:
        channel_display = getattr(channel, "mention", None)
        if not channel_display and getattr(channel, "name", None):
            channel_display = f"#{channel.name}"
        dm_ok = await _dm_member(
            dm_destination, guild.name, dm_text, reason_text,
            guild_id=guild.id, channel_id=channel_id, message_id=message_id,
            message_content=evidence_content,
            channel_display=channel_display,
            created_at=getattr(message_for_evidence, "created_at", None),
            jump_url=getattr(message_for_evidence, "jump_url", None),
        )
        if action == "warn" and not dm_ok:
            return await fail("대상 유저에게 경고 DM을 보낼 수 없어 경고를 적용하지 못했습니다.")

    applied = action_label + note

    if action not in ("ok", "okg"):
        # 외부 조치가 성공한 즉시 검수를 확정해, 이후 점수/로그 오류가 나더라도
        # 같은 카드 재시도로 제재가 중복 실행되지 않게 한다.
        if review_id:
            await database.resolve_review(
                review_id, guild.id, "confirmed", admin.id, action_label,
                sanction={
                    "user_id": user_id,
                    "user_display": _member_ledger_display(member, user_id),
                    "action_type": {
                        "del": "DELETE", "warn": "WARNING",
                        "to1": "TIMEOUT", "to24": "TIMEOUT",
                        "kick": "KICK", "ban": "BAN",
                    }[action],
                    "reason": reason_text or "관리자 검수 확정",
                    "source": "review",
                    "expires_at": timeout_expires_at,
                    "issued_by_display": _member_ledger_display(admin, admin.id),
                    "dedupe_key": f"review:{review_id}:{action}",
                },
            )
        else:
            await database.record_sanction(
                guild.id, user_id, _member_ledger_display(member, user_id),
                {
                    "del": "DELETE", "warn": "WARNING",
                    "to1": "TIMEOUT", "to24": "TIMEOUT",
                    "kick": "KICK", "ban": "BAN",
                }[action],
                reason_text or "관리자 검수 확정", "review",
                f"review-message:{message_id}:{action}",
                issued_by_id=admin.id,
                issued_by_display=_member_ledger_display(admin, admin.id),
                expires_at=timeout_expires_at,
            )
        points = config.VIOLATION_LEVEL_POINTS.get(level, 0)
        total = await database.add_points(guild.id, user_id, points)
        applied += f" · 점수 +{points} (누적 {total:.1f})"
        if not review_id:
            await database.log_violation(
                guild.id, user_id, channel_id, original_content, level,
                f"관리자 검수 확정: {reason_text}", action_label,
                provider="admin", needs_review=False, message_id=message_id,
            )
        public_action = {"del": "DELETE", "warn": "WARN", "to1": "TIMEOUT",
                         "to24": "TIMEOUT", "kick": "KICK", "ban": "BAN"}[action]
        await send_public_sanction_log(guild, level, rule, public_action)
    else:  # 관리자가 오탐(정상)으로 확정
        server_wide = action == "okg"
        channel_for_scope = await _resolve_review_learning_scope(
            guild, review_id, channel_id
        )
        content_for_learning = "" if original_content == "(내용 없음)" else original_content
        try:
            if review_id:
                learned = await learning.record_review_false_positive(
                    review_id, guild.id, channel_for_scope, admin.id,
                    action_label, server_wide=server_wide,
                )
            else:
                learned = await learning.record_false_positive(
                    guild.id, channel_for_scope, content_for_learning, level,
                    reason_text, admin.id, server_wide=server_wide,
                )
        except Exception as e:
            print(f"[learning] 오탐 검수 저장 실패: {e}")
            return await fail("오탐 검수와 학습 저장에 실패했습니다. 다시 시도해주세요.")
        if not learned:
            return await fail("학습할 원문을 찾지 못했습니다. 검수 로그를 확인해주세요.")
        scope_text = "서버 전체" if server_wide else "현재 채널"
        applied += f" · 오탐 학습됨 ({scope_text}, 규칙 #{learned['id'] if isinstance(learned, dict) else learned})"

    if dm_text and not dm_ok:
        applied += " · DM 전달 실패"

    return True, applied


def _finalize_review_embed(embed: discord.Embed, action: str, result_text: str, admin):
    is_false_positive = action in ("ok", "okg")
    embed.title = "✅ 검수 완료 — 정상 (오탐)" if is_false_positive else "✅ 검수 완료 — 조치 적용"
    embed.colour = discord.Color.green() if is_false_positive else discord.Color.dark_grey()
    embed.description = None
    embed.add_field(name="검수 결과", value=f"{result_text}\n처리자: {admin.mention}", inline=False)


async def _publish_review_completion(log_message: discord.Message,
                                     embed: discord.Embed) -> str:
    """검수 완료 카드를 남기되 오래된 메시지 PATCH 제한을 선제적으로 피한다."""
    created_at = getattr(log_message, "created_at", None)
    is_old = (
        created_at is not None
        and created_at <= discord.utils.utcnow() - datetime.timedelta(minutes=55)
    )

    if not is_old:
        try:
            await log_message.edit(embed=embed, view=None)
            return "edited"
        except (discord.Forbidden, discord.HTTPException) as e:
            # Discord가 최근 카드에도 일시적인 편집 제한을 반환할 수 있으므로 아래의
            # 새 카드 게시 경로로 전환한다. 관리자 조치 자체는 이미 DB에 확정된 상태다.
            print(f"[review] 완료 카드 편집 실패, 새 카드로 대체: {type(e).__name__}")

    channel = getattr(log_message, "channel", None)
    if channel is None or not hasattr(channel, "send"):
        return "failed"
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[review] 완료 카드 대체 게시 실패: {type(e).__name__}")
        return "failed"

    # 새 완료 카드가 안전하게 게시된 뒤에만 버튼이 남은 기존 카드를 정리한다.
    # 삭제 실패 시에도 DB의 claim_review가 중복 조치를 차단한다.
    try:
        await log_message.delete()
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[review] 처리된 기존 카드 정리 실패: {type(e).__name__}")
        return "reposted"
    return "replaced"


class _DangerConfirmView(discord.ui.View):
    """킥/밴은 되돌리기 어려우므로 한 번 더 확인을 거친다 (60초 안에 응답)."""

    def __init__(self, log_message: discord.Message, action: str,
                 channel_id: int, message_id: int, user_id: int, review_id: int = 0):
        super().__init__(timeout=60)
        self.log_message = log_message
        self.action = action
        self.channel_id = channel_id
        self.message_id = message_id
        self.user_id = user_id
        self.review_id = review_id

    @discord.ui.button(label="예, 실행합니다", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok, text = await _apply_review_action(
            interaction.guild, self.log_message, interaction.user,
            self.action, self.channel_id, self.message_id, self.user_id, self.review_id,
        )
        if not ok:
            await interaction.edit_original_response(content=f"⚠️ {text}", view=None)
            return
        embed = self.log_message.embeds[0]
        _finalize_review_embed(embed, self.action, text, interaction.user)
        card_status = await _publish_review_completion(self.log_message, embed)
        card_note = ""
        if card_status == "failed":
            card_note = "\n⚠️ 조치는 완료됐지만 검토 카드 화면을 갱신하지 못했습니다."
        elif card_status == "reposted":
            card_note = "\nℹ️ 새 완료 카드는 게시됐으며 기존 버튼은 DB에서 재사용이 차단됩니다."
        await interaction.edit_original_response(
            content=f"✅ 완료: {text}{card_note}", view=None
        )
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="취소했습니다.", view=None)
        self.stop()


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """검수 카드의 버튼 클릭 처리. custom_id 기반이라 봇 재시작 후에도 동작한다."""
    if interaction.type != discord.InteractionType.component or interaction.guild is None:
        return
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith(_REVIEW_BTN_PREFIX):
        return
    try:
        parts = custom_id[len(_REVIEW_BTN_PREFIX):].split(":")
        if len(parts) == 4:  # 이전 버전 카드 호환
            action, raw_ch, raw_msg, raw_user = parts
            raw_review = "0"
        elif len(parts) == 5:
            action, raw_ch, raw_msg, raw_user, raw_review = parts
        else:
            return
        channel_id, message_id, user_id = int(raw_ch), int(raw_msg), int(raw_user)
        review_id = int(raw_review)
    except ValueError:
        return
    if action not in _REVIEW_ACTION_LABEL:
        return

    # 권한 확인: 검수 카드의 모든 버튼은 관리자 전용 (명령어와 동일한 서버 정책).
    # 관리자는 킥/밴 권한을 포함한 모든 권한을 가지므로 별도 세분화가 필요 없다.
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is None or not perms.administrator:
        await interaction.response.send_message("⛔ 검수 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True)
        return

    if action in ("kick", "ban"):
        label = "킥(추방)" if action == "kick" else "밴(영구 차단)"
        await interaction.response.send_message(
            f"<@{user_id}> 유저를 정말 **{label}** 하시겠어요?",
            view=_DangerConfirmView(
                interaction.message, action, channel_id, message_id, user_id, review_id
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    ok, text = await _apply_review_action(
        interaction.guild, interaction.message, interaction.user,
        action, channel_id, message_id, user_id, review_id,
    )
    if not ok:
        await interaction.followup.send(f"⚠️ {text}", ephemeral=True)
        return
    embed = interaction.message.embeds[0]
    _finalize_review_embed(embed, action, text, interaction.user)
    card_status = await _publish_review_completion(interaction.message, embed)
    if card_status == "failed":
        await interaction.followup.send(
            "✅ 관리자 조치는 완료됐지만 Discord 제한으로 검토 카드 화면을 갱신하지 못했습니다. "
            "같은 검수 건의 중복 조치는 DB에서 차단됩니다.",
            ephemeral=True,
        )
    elif card_status == "replaced":
        await interaction.followup.send(
            "✅ 처리가 완료됐습니다. 오래된 검토 카드는 Discord 편집 제한을 피하도록 "
            "새 완료 카드로 교체했습니다.",
            ephemeral=True,
        )
    elif card_status == "reposted":
        await interaction.followup.send(
            "✅ 처리가 완료되어 새 완료 카드를 게시했습니다. 기존 카드의 버튼을 다시 눌러도 "
            "중복 조치는 적용되지 않습니다.",
            ephemeral=True,
        )


async def handle_violation(message: discord.Message, level: str, reason_text: str,
                            rule_violated: str = "-", provider: str = "filter",
                            visual_context: dict | None = None):
    """위반으로 판정된 메시지에 대해 점수 부여 + 조치 실행 + 로그를 공통 처리한다."""
    if level == "NONE":
        return

    # 큐 대기 중 수정·삭제된 메시지를 과거 내용으로 제재하지 않는다.
    try:
        latest = await message.channel.fetch_message(message.id)
    except discord.NotFound:
        return
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[moderation] 제재 전 메시지 재확인 실패로 안전하게 보류: {e}")
        return
    if (latest.content != message.content
            or vision.attachment_fingerprint(latest) != vision.attachment_fingerprint(message)):
        return
    message = latest

    # 최신 원문이 현재 채널에 적용되는 오탐 규칙과 일치하면 처리하지 않는다.
    if (not vision.has_image_attachments(message)
            and await learning.is_known_false_positive(
                message.guild.id, message.channel, message.content)):
        return

    points_to_add = config.VIOLATION_LEVEL_POINTS.get(level, 0)

    # 수동 검수 모드: 감지 결과만 관리자에게 보고하고 여기서 끝낸다 (config.MANUAL_REVIEW_MODE 참고)
    if config.MANUAL_REVIEW_MODE:
        await _handle_violation_review_only(
            message, level, reason_text, rule_violated, provider, points_to_add,
            visual_context=visual_context,
        )
        return

    # [점수 정책] 실제로 집행된 제재만 누적 점수에 반영한다.
    # 예전에는 점수를 먼저 올리고 조치를 실행해서, 권한 부족 등으로 조치가 실패해도 점수는
    # 남았다. 그러면 유저는 아무 제재도 받지 않았는데 다음 위반에서 더 높은 단계가 적용된다.
    # 그래서 조치 단계는 "성공하면 될 점수"로 미리 정하고, 실행에 성공한 뒤에만 확정 저장한다.
    current_points = await database.get_points(message.guild.id, message.author.id)
    prospective_points = current_points + points_to_add
    action, duration, downgraded = determine_action(prospective_points, level)

    reason = f"[{level}] 규칙 {rule_violated} 위반 - {reason_text}"
    if downgraded:
        reason += " (⚠️ 킥/밴은 자동 실행하지 않는 정책에 따라 타임아웃으로 조정됨, 관리자 검토 필요)"
    action_ok, action_detail = await apply_action(message, action, duration, reason)
    recorded_action = action if action_ok else f"{action}_FAILED"

    if action_ok:
        total_points = await database.add_points(message.guild.id, message.author.id, points_to_add)
    else:
        total_points = current_points
        action_detail += f" · 조치 실패로 점수 +{points_to_add} 미반영 (누적 {current_points:.1f} 유지)"

    await database.log_violation(
        message.guild.id, message.author.id, message.channel.id,
        message.content, level, reason_text, recorded_action, provider,
        needs_review=(downgraded and action_ok), message_id=message.id,
    )
    if action_ok and action != "NONE":
        await database.record_sanction(
            message.guild.id, message.author.id,
            _member_ledger_display(message.author, message.author.id),
            action,
            reason_text or f"규칙 {rule_violated} 위반", "automatic",
            f"automatic:{message.id}:{action}",
            issued_by_id=getattr(bot.user, "id", None),
            issued_by_display=str(bot.user) if bot.user else "BB봇",
            expires_at=(
                time.time() + float(duration) * 60
                if action == "TIMEOUT" and duration else None
            ),
        )

    embed = discord.Embed(
        title=(
            "❌ 자동 제재 실행 실패"
            if not action_ok
            else ("🚨 자동 제재 발동" if not downgraded else "🚨⚠️ 자동 제재 발동 (관리자 검토 필요)")
        ),
        color=discord.Color.red() if not action_ok else (discord.Color.gold() if downgraded else (
            discord.Color.orange() if level in ("MINOR", "MODERATE") else discord.Color.red()
        )),
    )
    embed.add_field(name="유저", value=f"{message.author.mention} ({message.author.id})", inline=False)
    embed.add_field(name="채널", value=message.channel.mention, inline=True)
    embed.add_field(name="위반 등급", value=level, inline=True)
    embed.add_field(name="누적 점수", value=f"{total_points:.1f}", inline=True)
    embed.add_field(
        name="조치",
        value=action if action_ok else f"{action} 실행 실패",
        inline=True,
    )
    embed.add_field(name="실행 결과", value=action_detail[:1024], inline=False)
    embed.add_field(name="판단 주체", value=_PROVIDER_LABEL.get(provider, provider), inline=True)
    if downgraded:
        embed.add_field(
            name="⚠️ 하향 조정 안내",
            value=f"정책상 킥/밴은 자동 실행하지 않아 {duration}분 타임아웃으로 대체 적용됨. "
                  f"`!BB 검토대기`에서 확인 후 관리자가 직접 킥/밴 여부를 결정해주세요.",
            inline=False,
        )
    embed.add_field(name="사유", value=reason_text or "-", inline=False)
    embed.add_field(name="원문", value=(message.content[:500] or "(내용 없음)"), inline=False)

    mention = config.ADMIN_REVIEW_MENTION if (downgraded and config.FLAG_DOWNGRADED_FOR_REVIEW and config.ADMIN_REVIEW_MENTION) else None
    await send_log(message.guild, embed, mention=mention)

    # 익명화된 공개 제재 로그 (운영 투명성) — 유저 식별 정보 없이 등급/유형/조치만 게시
    if action_ok:
        await send_public_sanction_log(message.guild, level, rule_violated, action)


# AI 전량 장애 감지: Gemini/Groq/Ollama가 모두 실패하면 메시지를 SQLite 보류 큐에 넣지만,
# 복구 전까지 판단이 지연되는 사실을 관리자가 놓치지 않도록 연속 실패 시 로그 채널에 알린다.
# 상태는 서버(guild)별로 따로 센다 — 전역 카운터로 두면 여러 서버에 들어가 있을 때
# 한 서버의 성공이 다른 서버의 연속 실패를 초기화하고, 알림도 엉뚱한 서버로 갈 수 있다.
# {guild_id: {"streak": int, "last_alert": datetime | None}}
_ai_outage_state: dict[int, dict] = {}


async def _track_ai_outage(guild: discord.Guild, result):
    state = _ai_outage_state.setdefault(
        guild.id, {"streak": 0, "last_alert": None, "last_category": None}
    )

    if result.provider != "none":
        if state["streak"] >= config.AI_OUTAGE_ALERT_THRESHOLD:
            print(f"✅ AI 판단이 복구되었습니다 (길드 {guild.id}).")
        state["streak"] = 0
        state["last_category"] = None
        return

    state["streak"] += 1
    state["last_category"] = getattr(result, "failure_category", None) or "unknown"
    if state["streak"] < config.AI_OUTAGE_ALERT_THRESHOLD:
        return

    now = discord.utils.utcnow()
    cooldown = datetime.timedelta(minutes=config.AI_OUTAGE_ALERT_COOLDOWN_MINUTES)
    if state["last_alert"] and now - state["last_alert"] < cooldown:
        return
    state["last_alert"] = now

    if config.OLLAMA_REALTIME_FALLBACK:
        chain_text = "Gemini · Groq · 로컬 Ollama 판단이"
        checklist = (
            "1. API 무료 한도 초과 여부 (Gemini는 한국시간 오후 4시경 리셋)\n"
            "2. .env의 GEMINI_API_KEY / GROQ_API_KEY 유효 여부\n"
            f"3. 로컬 Ollama 실행 여부 — `ollama serve` 후 `ollama list`에 "
            f"`{config.OLLAMA_MODEL}`이 있는지 확인\n"
            "4. 봇 콘솔 창의 오류 메시지"
        )
    else:
        chain_text = "Gemini와 Groq 판단이"
        checklist = (
            "1. API 무료 한도 초과 여부 (Gemini는 한국시간 오후 4시경 리셋)\n"
            "2. .env의 GEMINI_API_KEY / GROQ_API_KEY 유효 여부\n"
            "3. 봇 콘솔 창의 오류 메시지\n"
            "4. `config.OLLAMA_REALTIME_FALLBACK`을 켜면 한도 없는 로컬 Ollama가 "
            "마지막 폴백으로 동작합니다"
        )

    embed = discord.Embed(
        title="🔴 AI 판단 장애 감지",
        description=(
            f"{chain_text} **{state['streak']}회 연속 실패**했습니다.\n"
            f"최근 실패 유형: `{state['last_category']}`\n"
            "실패한 메시지는 **내구성 재검사 큐에 보류**되며 제공자가 복구되면 다시 판단합니다. "
            "복구 전까지 신규 판단은 지연되고 현재 즉시 감시는 키워드 필터만 동작합니다.\n\n"
            f"확인할 것:\n{checklist}"
        ),
        color=discord.Color.red(),
        timestamp=now,
    )
    await send_log(guild, embed, mention=config.ADMIN_REVIEW_MENTION or None)


async def ai_worker(worker_id: int):
    """큐에서 메시지를 꺼내 캐시 확인 후 필요하면 AI(Gemini→Groq→Ollama)로 판단하는 워커."""
    while True:
        message, enqueued_at, processing_key, settle_for_burst = await _message_queue.get()
        try:
            queue_age = time.monotonic() - enqueued_at
            if queue_age > config.MAX_QUEUE_AGE_SECONDS:
                print(f"[worker-{worker_id}] {queue_age:.1f}초 지난 메시지를 안전하게 폐기했습니다.")
                _note_drop(message.guild, expired=True)
                continue
            # 관리자가 오탐으로 확정했던 내용과 동일하면 AI 호출 없이 즉시 통과
            # (재오탐 방지 + 무료 API 한도 절약)
            if (not vision.has_image_attachments(message)
                    and _reply_parent_id(message) is None
                    and not _has_recent_same_author_fragment(message)
                    and await learning.is_known_false_positive(
                        message.guild.id, message.channel, message.content)):
                # 큐 대기 중 수정된 메시지가 과거 원문 기준으로 통과하지 않게 재확인한다.
                try:
                    latest = await message.channel.fetch_message(message.id)
                except discord.NotFound:
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    latest = None
                if latest is not None and latest.content == message.content:
                    await database.delete_moderation_retry_for_message(
                        message.guild.id, message.id
                    )
                    continue
                if latest is not None:
                    message = latest

            # 채널별 특수 규칙 (예: 물물교환 채널은 거래 글이 정상). 캐시 키에도 섞어서
            # 같은 문구가 규칙이 다른 채널의 판단 결과를 재사용하지 않게 한다.
            channel_note = get_channel_note(message.channel)
            barter_context = _is_barter_channel(message.channel)
            conversation_context, burst_context = await _conversation_context_for_message(
                message, settle=settle_for_burst, barter_context=barter_context
            )
            # 같은 작성자가 2.5초 안에 후속 조각을 보냈고 그 최신 조각도 검사 중이면
            # 이전 조각은 판단하지 않는다. 최신 조각 하나가 전체 발화를 대표해 중복 카드를 막는다.
            if settle_for_burst and _newer_same_author_is_processing(message):
                continue
            try:
                visual_context = await vision.analyze_message_attachments(message)
            except vision.VisionAnalysisUnavailable as error:
                result = moderator.ModerationResult(
                    "NONE", "NONE", f"이미지 분석 실패(재검사 보류): {error.category}",
                    provider="none", failure_category=f"vision:{error.category}",
                )
                await _track_ai_outage(message.guild, result)
                if await _defer_ai_failure(message, result, f"worker-{worker_id}"):
                    continue
                continue
            cache_context = channel_note or ""
            if barter_context:
                # 이전 단일 문장 기준 캐시와 새 대화 증거 기준 캐시를 분리한다.
                cache_context += "\x00barter-conversation-v2"
            if conversation_context:
                # 같은 한 줄도 앞뒤 거래 문맥에 따라 정상/위반이 달라질 수 있으므로 캐시를 분리한다.
                cache_context += "\x00" + json.dumps(
                    conversation_context, ensure_ascii=False, sort_keys=True
                )
            if visual_context:
                cache_context += "\x00vision-v1\x00" + json.dumps(
                    visual_context, ensure_ascii=False, sort_keys=True
                )
            cached = cache.get(message.content, context=cache_context)
            if cached is not None:
                level, rule_violated, reason_text, provider = cached
                await database.delete_moderation_retry_for_message(
                    message.guild.id, message.id
                )
                await handle_violation(
                    message, level, f"(캐시된 판단) {reason_text}",
                    rule_violated=rule_violated, provider=provider,
                    visual_context=visual_context,
                )
                continue

            # 과거 오탐 사례를 프롬프트에 포함해 같은 유형의 오탐을 줄인다
            fp_examples = await learning.get_prompt_examples(message.guild.id, message.channel)
            async with _ai_semaphore:
                split_assessment = None
                if settle_for_burst and burst_context:
                    try:
                        split_assessment = await moderator.assess_split_utterance(
                            message.content, burst_context
                        )
                    except Exception as error:
                        print(
                            "[split-context] 분할 발화 전용 판단 실패, 일반 판단 유지: "
                            f"{type(error).__name__}"
                        )
                result = await classify_message(message.content, channel_note=channel_note,
                                                fp_examples=fp_examples,
                                                conversation_context=conversation_context,
                                                barter_context=barter_context,
                                                visual_context=visual_context)
            result = moderator.apply_split_utterance_guard(result, split_assessment)

            # 판단 실패(provider="none")는 캐시하지 않는다 — 캐시하면 AI가 복구된 뒤에도
            # 같은 내용의 메시지가 캐시 유효시간 동안 계속 무검사 통과하게 됨
            if result.provider != "none":
                cache.set(
                    message.content, result.level, result.rule_violated,
                    result.reason, result.provider, context=cache_context,
                )
            await _track_ai_outage(message.guild, result)
            if await _defer_ai_failure(message, result, f"worker-{worker_id}"):
                continue
            if result.provider != "none":
                await database.delete_moderation_retry_for_message(
                    message.guild.id, message.id
                )
            await handle_violation(
                message, result.level, result.reason, result.rule_violated,
                provider=result.provider, visual_context=visual_context,
            )
        except Exception as e:
            print(f"[worker-{worker_id}] 처리 중 오류: {e}")
        finally:
            _processing_keys.discard(processing_key)
            _message_queue.task_done()


async def _defer_ai_failure(message: discord.Message, result, source: str) -> bool:
    """전량 장애 결과를 영구 통과시키지 않고 재검사 큐에 저장했으면 True를 반환한다."""
    if result.provider != "none" or not config.AI_RETRY_ENABLED:
        return False
    await database.enqueue_moderation_retry(
        message.guild.id,
        message.channel.id,
        message.id,
        result.failure_category or "unknown",
        config.AI_RETRY_INITIAL_DELAY_SECONDS,
    )
    print(f"[{source}] AI 판단 실패 메시지 {message.id}를 재검사 큐에 보류했습니다.")
    return True


def _ai_retry_delay(attempts: int) -> float:
    multiplier = 2 ** min(max(0, attempts), 8)
    return min(
        config.AI_RETRY_MAX_DELAY_SECONDS,
        config.AI_RETRY_INITIAL_DELAY_SECONDS * multiplier,
    )


async def ai_retry_worker():
    """SQLite에 보류한 AI 전량 장애 메시지를 제공자 복구 후 다시 판단한다."""
    while True:
        rows = await database.get_due_moderation_retries(config.AI_RETRY_BATCH_SIZE)
        if not rows:
            await asyncio.sleep(config.AI_RETRY_POLL_SECONDS)
            continue

        for retry_id, guild_id, channel_id, message_id, attempts, _ in rows:
            guild = bot.get_guild(guild_id)
            if guild is None:
                await database.delete_moderation_retry(retry_id)
                continue
            channel = guild.get_channel(channel_id) or bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except discord.NotFound:
                    await database.delete_moderation_retry(retry_id)
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    await database.reschedule_moderation_retry(
                        retry_id, attempts + 1, "discord_channel_unavailable",
                        _ai_retry_delay(attempts + 1),
                    )
                    continue

            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                await database.delete_moderation_retry(retry_id)
                continue
            except (discord.Forbidden, discord.HTTPException):
                await database.reschedule_moderation_retry(
                    retry_id, attempts + 1, "discord_message_unavailable",
                    _ai_retry_delay(attempts + 1),
                )
                continue

            if getattr(message.author, "bot", False):
                await database.delete_moderation_retry(retry_id)
                continue
            processing_key = _message_processing_key(message)
            if processing_key in _processing_keys:
                await database.reschedule_moderation_retry(
                    retry_id, attempts, "already_processing", config.AI_RETRY_POLL_SECONDS
                )
                continue

            _processing_keys.add(processing_key)
            try:
                if (not vision.has_image_attachments(message)
                        and _reply_parent_id(message) is None
                        and not _has_recent_same_author_fragment(message)
                        and await learning.is_known_false_positive(
                            guild.id, message.channel, message.content)):
                    await database.delete_moderation_retry(retry_id)
                    continue

                channel_note = get_channel_note(message.channel)
                barter_context = _is_barter_channel(message.channel)
                conversation_context, _ = await _conversation_context_for_message(
                    message, settle=False, barter_context=barter_context
                )
                try:
                    visual_context = await vision.analyze_message_attachments(message)
                except vision.VisionAnalysisUnavailable as error:
                    next_attempt = attempts + 1
                    await database.reschedule_moderation_retry(
                        retry_id, next_attempt, f"vision:{error.category}",
                        _ai_retry_delay(next_attempt),
                    )
                    continue
                cache_context = channel_note or ""
                if barter_context:
                    cache_context += "\x00barter-conversation-v2"
                if conversation_context:
                    cache_context += "\x00" + json.dumps(
                        conversation_context, ensure_ascii=False, sort_keys=True
                    )
                if visual_context:
                    cache_context += "\x00vision-v1\x00" + json.dumps(
                        visual_context, ensure_ascii=False, sort_keys=True
                    )
                cached = cache.get(message.content, context=cache_context)
                if cached is not None:
                    level, rule_violated, reason_text, provider = cached
                    await database.delete_moderation_retry(retry_id)
                    await handle_violation(
                        message, level, f"(재검사·캐시된 판단) {reason_text}",
                        rule_violated=rule_violated, provider=provider,
                        visual_context=visual_context,
                    )
                    continue

                fp_examples = await learning.get_prompt_examples(guild.id, message.channel)
                async with _ai_semaphore:
                    result = await classify_message(
                        message.content,
                        channel_note=channel_note,
                        fp_examples=fp_examples,
                        conversation_context=conversation_context,
                        barter_context=barter_context,
                        visual_context=visual_context,
                    )
                await _track_ai_outage(guild, result)
                if result.provider == "none":
                    next_attempt = attempts + 1
                    await database.reschedule_moderation_retry(
                        retry_id,
                        next_attempt,
                        result.failure_category or "unknown",
                        _ai_retry_delay(next_attempt),
                    )
                    continue

                cache.set(
                    message.content, result.level, result.rule_violated,
                    result.reason, result.provider, context=cache_context,
                )
                await database.delete_moderation_retry(retry_id)
                await handle_violation(
                    message, result.level, f"(장애 복구 후 재판단) {result.reason}",
                    result.rule_violated, provider=result.provider,
                    visual_context=visual_context,
                )
            except Exception as error:
                next_attempt = attempts + 1
                await database.reschedule_moderation_retry(
                    retry_id, next_attempt, type(error).__name__,
                    _ai_retry_delay(next_attempt),
                )
                print(f"[retry-worker] 메시지 {message_id} 재검사 중 오류: {type(error).__name__}")
            finally:
                _processing_keys.discard(processing_key)


# ── KPI 집계 보고·외부 대시보드 동기화 ─────────────────────────────

def _kpi_channel_labels(guild: discord.Guild) -> dict[int, str]:
    """집계 화면에 쓸 현재 채널 이름을 만든다. 삭제된 채널은 DB의 마지막 이름을 쓴다."""
    labels = {int(channel.id): f"#{channel.name}" for channel in guild.channels}
    for thread in guild.threads:
        parent_name = getattr(getattr(thread, "parent", None), "name", None)
        labels[int(thread.id)] = f"#{parent_name or thread.name}"
    return labels


def _kpi_row_channel_names(guild: discord.Guild, row: dict) -> tuple[str, str]:
    channel = guild.get_channel_or_thread(int(row["channel_id"]))
    if channel is None:
        name = row.get("channel_name") or "삭제·이전된 채널"
        group = row.get("channel_group") or name
        return str(name), str(group)
    name = getattr(channel, "name", None) or row.get("channel_name") or "이름 미확인 채널"
    parent = getattr(channel, "parent", None)
    group = getattr(parent, "name", None) or row.get("channel_group") or name
    return str(name), str(group)


async def kpi_sync_worker():
    """비식별 KPI 이벤트를 내구성 대기열에서 사이트로 전송하고 실패 시 재시도한다."""
    global _kpi_http_client, _kpi_backfill_done
    if not (KPI_DASHBOARD_INGEST_URL and KPI_DASHBOARD_INGEST_TOKEN):
        return

    if not _kpi_backfill_done:
        review_total = 0
        audit_total = 0
        sanction_total = 0
        for guild in bot.guilds:
            review_total += await database.backfill_kpi_sync_outbox(int(guild.id))
            audit_total += await database.backfill_audit_kpi_sync_outbox(int(guild.id))
            sanction_total += await database.backfill_sanction_sync_outbox(int(guild.id))
        _kpi_backfill_done = True
        if review_total or audit_total or sanction_total:
            print(
                f"[kpi-sync] 기존 검수 {review_total}건·감사 {audit_total}건·"
                f"제재 {sanction_total}건을 "
                "동기화 대기열에 추가했습니다."
            )

    _kpi_http_client = httpx.AsyncClient(timeout=15.0)
    last_operations_sync = 0.0
    while True:
        await database.expire_elapsed_timeouts()
        rows = await database.get_due_kpi_sync_records(config.KPI_SYNC_BATCH_SIZE)
        audit_rows = await database.get_due_audit_kpi_sync_records(
            max(1, config.KPI_SYNC_BATCH_SIZE // 2)
        )
        sanction_rows = await database.get_due_sanction_sync_records(
            max(1, config.KPI_SYNC_BATCH_SIZE)
        )
        operations_due = time.monotonic() - last_operations_sync >= 300
        if not rows and not audit_rows and not sanction_rows and not operations_due:
            await asyncio.sleep(config.KPI_SYNC_INTERVAL_SECONDS)
            continue

        review_ids = [int(row["review_id"]) for row in rows]
        audit_run_ids = [int(row["audit_run_id"]) for row in audit_rows]
        sanction_ids = [int(row["sanction_id"]) for row in sanction_rows]
        try:
            events = []
            for row in rows:
                guild = bot.get_guild(int(row["guild_id"]))
                if guild is None:
                    channel_name = row.get("channel_name") or "삭제·이전된 채널"
                    channel_group = row.get("channel_group") or channel_name
                else:
                    channel_name, channel_group = _kpi_row_channel_names(guild, row)
                events.append(kpi.build_sync_event(row, channel_name, channel_group))
            audit_events = [kpi.build_sync_audit_event(row) for row in audit_rows]
            sanction_events = [kpi.build_sync_sanction(row) for row in sanction_rows]
            operations = []
            if operations_due:
                for guild in bot.guilds:
                    counts = await database.get_kpi_operational_counts(int(guild.id))
                    operations.append(kpi.build_sync_operations(int(guild.id), counts))

            response = await _kpi_http_client.post(
                KPI_DASHBOARD_INGEST_URL,
                headers={"Authorization": f"Bearer {KPI_DASHBOARD_INGEST_TOKEN}"},
                json={
                    "schema_version": 2,
                    "events": events,
                    "audit_runs": audit_events,
                    "operations": operations,
                    "sanctions": sanction_events,
                },
            )
            response.raise_for_status()
            await database.mark_kpi_sync_complete(review_ids)
            await database.mark_audit_kpi_sync_complete(audit_run_ids)
            await database.mark_sanction_sync_complete(sanction_ids)
            if operations_due:
                last_operations_sync = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            attempt_values = [int(row.get("sync_attempts", 0)) for row in rows]
            attempt_values.extend(
                int(row.get("sync_attempts", 0)) for row in audit_rows
            )
            attempt_values.extend(
                int(row.get("sync_attempts", 0)) for row in sanction_rows
            )
            attempts = max(attempt_values, default=0) + 1
            delay = min(
                config.KPI_SYNC_MAX_RETRY_SECONDS,
                config.KPI_SYNC_INTERVAL_SECONDS * (2 ** min(attempts, 4)),
            )
            await database.reschedule_kpi_sync(review_ids, type(error).__name__, delay)
            await database.reschedule_audit_kpi_sync(
                audit_run_ids, type(error).__name__, delay
            )
            await database.reschedule_sanction_sync(
                sanction_ids, type(error).__name__, delay
            )
            print(
                f"[kpi-sync] 카드 {len(rows)}건·감사 {len(audit_rows)}건·"
                f"제재 {len(sanction_rows)}건 전송 실패"
                f"({type(error).__name__}), {delay:g}초 후 재시도"
            )
        await asyncio.sleep(0)


def _kpi_report_embed(summary: dict) -> discord.Embed:
    cards = summary["cards"]
    embed = discord.Embed(
        title=f"📊 BB봇 KPI — {summary['period']['label']}",
        description="\n".join(kpi.concise_report_lines(summary)),
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.now(_KST),
    )
    review_time = summary["review_time_hours"]
    median = "-" if review_time["median"] is None else f"{review_time['median']:.1f}시간"
    p90 = "-" if review_time["p90"] is None else f"{review_time['p90']:.1f}시간"
    embed.add_field(
        name="검수 운영",
        value=(f"해결률 {cards['resolution_percent'] if cards['resolution_percent'] is not None else '-'}%\n"
               f"검수 중앙값 {median} · P90 {p90}"),
        inline=True,
    )
    operations = summary["operations"]
    embed.add_field(
        name="현재 미처리·복구 상태",
        value=(f"24시간 초과 {operations['pending_over_24h']}건\n"
               f"72시간 초과 {operations['pending_over_72h']}건\n"
               f"AI 재판단 대기 {operations['ai_retry_queue']}건"),
        inline=True,
    )
    audit = summary["audit"]
    audit_rate = "-" if audit["flag_rate_percent"] is None else f"{audit['flag_rate_percent']:.1f}%"
    audit_success = (
        "-" if audit["successful_channel_percent"] is None
        else f"{audit['successful_channel_percent']:.1f}%"
    )
    embed.add_field(
        name="배치 감사 커버리지",
        value=(f"{audit['runs']}회 · 메시지 {audit['reviewed_messages']}건\n"
               f"의심 감지율 {audit_rate} · 채널 성공률 {audit_success}"),
        inline=False,
    )
    if KPI_DASHBOARD_PUBLIC_URL:
        embed.add_field(
            name="상시 대시보드",
            value=f"[관리자 KPI 대시보드 열기]({KPI_DASHBOARD_PUBLIC_URL})",
            inline=False,
        )
    embed.set_footer(text="정탐·오탐은 관리자 카드 검수 결과 기준 · 사용자/메시지 원문 비공개")
    return embed


async def _send_kpi_report(guild: discord.Guild, period: kpi.KpiPeriod,
                           *, record_delivery: bool) -> discord.Message | None:
    summary = await kpi.get_period_summary(
        int(guild.id), period, _kpi_channel_labels(guild)
    )
    channel = guild.get_channel_or_thread(KPI_REPORT_CHANNEL_ID) if KPI_REPORT_CHANNEL_ID else None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        print(f"[kpi-report] 서버 {guild.id}의 KPI 보고 채널을 찾을 수 없습니다.")
        return None
    message = await channel.send(embed=_kpi_report_embed(summary))
    if record_delivery:
        await database.mark_kpi_report_delivered(
            int(guild.id), period.kind, period.key, int(channel.id), int(message.id)
        )
    return message


@tasks.loop(minutes=30)
async def kpi_report_task():
    """매월 1일 이후 월간 보고를 보충하고 분기·연간 경계에는 함께 정산한다."""
    now = datetime.datetime.now(_KST)
    if now.hour < config.KPI_REPORT_HOUR_KST:
        return
    kinds = ["month"]
    if now.month in {1, 4, 7, 10}:
        kinds.append("quarter")
    if now.month == 1:
        kinds.append("year")
    for guild in bot.guilds:
        for kind in kinds:
            period = kpi.completed_period(kind, now)
            if await database.kpi_report_was_delivered(int(guild.id), kind, period.key):
                continue
            try:
                await _send_kpi_report(guild, period, record_delivery=True)
            except (discord.Forbidden, discord.HTTPException) as error:
                print(f"[kpi-report] {period.key} 게시 실패: {type(error).__name__}")


_audit_sync_permission_warned: set[int] = set()


async def _sync_guild_audit_log(guild: discord.Guild) -> int:
    """Gateway 누락·재연결 공백을 마지막 영속 커서 이후 감사 로그로 보충한다."""
    cursor = await database.get_discord_audit_cursor(int(guild.id))
    try:
        if cursor is None:
            # 최초 도입 시 과거 전체를 다시 수집하지 않고 현재 끝을 기준점으로 잡는다.
            async for entry in guild.audit_logs(limit=1):
                await database.advance_discord_audit_cursor(guild.id, entry.id)
            return 0

        processed = 0
        async for entry in guild.audit_logs(
            limit=500, after=discord.Object(id=cursor), oldest_first=True,
        ):
            if await _process_external_sanction_audit_entry(entry):
                processed += 1
            await database.advance_discord_audit_cursor(guild.id, entry.id)
        _audit_sync_permission_warned.discard(int(guild.id))
        return processed
    except discord.Forbidden:
        if int(guild.id) not in _audit_sync_permission_warned:
            print(
                f"[discord-audit] 서버 {guild.id}: 감사 로그 보기 권한이 없어 "
                "관리자·외부 봇 제재 보충 수집을 할 수 없습니다."
            )
            _audit_sync_permission_warned.add(int(guild.id))
        return 0
    except discord.HTTPException as error:
        print(f"[discord-audit] 서버 {guild.id} 보충 수집 실패: {type(error).__name__}")
        return 0


@tasks.loop(seconds=120)
async def discord_audit_sync_task():
    """2분마다 감사 로그 공백을 보충해 봇 재연결 중 제재 누락을 막는다."""
    for guild in bot.guilds:
        processed = await _sync_guild_audit_log(guild)
        if processed:
            print(f"[discord-audit] 서버 {guild.id} 제재 이력 {processed}건을 보충했습니다.")


# 매일 정해진 시각(한국 시간)에만 실행 — 시작 즉시 실행되는 hours= 방식과 달리
# 봇을 재시작해도 감사가 곧바로 돌지 않아 무료 API 한도를 아낀다.
_KST = datetime.timezone(datetime.timedelta(hours=9))


@tasks.loop(time=datetime.time(hour=config.BATCH_RUN_HOUR_KST, tzinfo=_KST))
async def batch_audit_task():
    """등록된 감시 채널들을 매일 정해진 시각에 감사해 리포트를 만든다 (조치 없음, 리포트만)."""
    if not config.WATCHED_CHANNEL_IDS:
        return
    for guild in bot.guilds:
        try:
            await run_full_audit(guild, backend=config.BATCH_BACKEND, on_flagged=_batch_review_callback())
        except Exception as e:
            print(f"[batch_audit_task] 길드 {guild.id} 감사 중 오류: {e}")


@bot.event
async def close():
    """진행 중인 자체 작업을 정리한 뒤 Discord와 공유 HTTP 연결을 닫는다."""
    global _workers_started

    # tasks.loop는 다음 실행 시각을 기다리는 내부 Task를 갖는다. Discord 연결을 먼저
    # 닫으면 종료 도중 감사가 시작될 수 있으므로 가장 먼저 중단한다.
    if batch_audit_task.is_running():
        batch_audit_task.cancel()
    if kpi_report_task.is_running():
        kpi_report_task.cancel()
    if discord_audit_sync_task.is_running():
        discord_audit_sync_task.cancel()

    # ai_worker는 Queue.get()에서 계속 대기하므로 명시적으로 취소해야 정상 종료 시
    # "Task was destroyed but it is pending" 경고와 미완료 작업 잔존을 막을 수 있다.
    pending = [task for task in tuple(_background_tasks) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _workers_started = False

    if _kpi_http_client is not None:
        await _kpi_http_client.aclose()
    await vision.aclose_http_client()
    await moderator.aclose_http_client()
    await commands.Bot.close(bot)


@bot.event
async def on_ready():
    global _workers_started
    await database.init_db()
    redacted = await database.redact_expired_violation_content(
        config.VIOLATION_CONTENT_RETENTION_DAYS
    )
    if redacted:
        print(f"[privacy] 보존 기간이 지난 위반 로그 원문 {redacted}건을 익명화했습니다.")
    removed_reports = prune_expired_reports(config.REPORT_RETENTION_DAYS)
    if removed_reports:
        print(f"[privacy] 보존 기간이 지난 감사 리포트 {removed_reports}개를 정리했습니다.")
    for guild in bot.guilds:
        backfilled_audits = await backfill_audit_metrics_from_reports(int(guild.id))
        if backfilled_audits:
            print(f"[kpi] 기존 감사 리포트 {backfilled_audits}건의 요약 지표를 백필했습니다.")
    orphaned_kpi = await database.prune_kpi_sync_outbox()
    if orphaned_kpi:
        print(f"[kpi] 고아 동기화 대기 항목 {orphaned_kpi}건을 정리했습니다.")
    migrated = await learning.initialize()
    if migrated:
        print(f"[learning] 기존 오탐 이력 {migrated}건을 범위 지정 규칙으로 이전했습니다.")
    repaired_scopes = await _repair_historical_thread_learning_scopes()
    if repaired_scopes:
        print(f"[learning] 개별 스레드 오탐 규칙 {repaired_scopes}건을 부모 채널 범위로 병합했습니다.")

    # on_ready는 네트워크 재연결 시마다 다시 불리므로, 워커는 최초 1회만 생성한다.
    # (중복 생성 시 같은 메시지를 여러 워커가 처리해 이중 제재가 발생할 수 있음)
    if not _workers_started:
        for i in range(config.MAX_CONCURRENT_AI_CALLS):
            _spawn(ai_worker(i))
        if config.AI_RETRY_ENABLED:
            _spawn(ai_retry_worker())
        if KPI_DASHBOARD_INGEST_URL and KPI_DASHBOARD_INGEST_TOKEN:
            _spawn(kpi_sync_worker())
        _workers_started = True

    if config.WATCHED_CHANNEL_IDS and not batch_audit_task.is_running():
        batch_audit_task.start()
    if KPI_REPORT_CHANNEL_ID and not kpi_report_task.is_running():
        kpi_report_task.start()
    if not discord_audit_sync_task.is_running():
        discord_audit_sync_task.start()
    for guild in bot.guilds:
        for warning in permission_warnings(guild.me):
            print(f"[permissions] 서버 {guild.id}: {warning}")
    mode = "수동 검수 모드 (감지만 하고 조치 없음)" if config.MANUAL_REVIEW_MODE else "자동 조치 모드"
    print(f"✅ 로그인 완료: {bot.user} ({mode}, AI 워커 {config.MAX_CONCURRENT_AI_CALLS}개, "
          f"배치 감사 {'활성' if config.WATCHED_CHANNEL_IDS else '비활성(채널 미등록)'})")


def _is_moderation_target(message: discord.Message) -> bool:
    """이 메시지가 자동 제재 검사 대상인지 확인한다 (새 메시지/수정 메시지 공통)."""
    # 봇 자신, 다른 봇, DM은 무시
    if message.author.bot or message.guild is None:
        return False
    # 봇 명령어(!BB ...)는 검사 대상이 아님 -> AI 한도 낭비 방지
    if message.content.startswith(COMMAND_PREFIXES):
        return False
    # 관리자(Administrator)만 자동 제재 대상에서 제외 — 명령어/검수 버튼과 동일한 정책.
    # 메시지 관리 권한만 가진 모더레이터는 일반 유저와 똑같이 검사받는다.
    # (웹훅 등으로 author가 Member가 아닐 수 있어 getattr로 안전하게 확인)
    perms = getattr(message.author, "guild_permissions", None)
    if perms and perms.administrator:
        return False
    return True


def _message_processing_key(message: discord.Message) -> tuple[int, int, str]:
    attachment_key = json.dumps(vision.attachment_fingerprint(message), ensure_ascii=False)
    return message.guild.id, message.id, f"{message.content}\x00{attachment_key}"


async def _handle_decided(message: discord.Message, result, processing_key):
    try:
        await handle_violation(message, result.level, result.reason)
    finally:
        _processing_keys.discard(processing_key)


def _note_drop(guild: discord.Guild, expired: bool = False):
    """
    검사되지 못하고 버려진 메시지를 집계하고, 누적이 임계치를 넘으면 로그 채널에 경고한다.
    콘솔 출력만으로는 장시간 감시 구멍을 놓치기 쉬워서 관리자에게 직접 알린다.
    """
    global _dropped_count, _expired_count, _last_drop_at

    if expired:
        _expired_count += 1
    else:
        _dropped_count += 1
    _last_drop_at = discord.utils.utcnow()

    total = _dropped_count + _expired_count
    if total % config.DROP_ALERT_THRESHOLD == 0 and guild is not None:
        _spawn(_alert_drops(guild))


async def _alert_drops(guild: discord.Guild):
    global _last_drop_alert_at

    now = discord.utils.utcnow()
    cooldown = datetime.timedelta(minutes=config.DROP_ALERT_COOLDOWN_MINUTES)
    if _last_drop_alert_at and now - _last_drop_alert_at < cooldown:
        return
    _last_drop_alert_at = now

    usage = _message_queue.qsize() / config.MAX_QUEUE_SIZE * 100
    embed = discord.Embed(
        title="🔴 메시지 누락 발생 (감시 구멍)",
        description=(
            f"검사하지 못하고 버린 메시지가 누적 **{_dropped_count + _expired_count}건**입니다.\n"
            f"- 큐 포화로 버림: {_dropped_count}건\n"
            f"- 대기 시간 초과로 폐기: {_expired_count}건\n"
            f"- 현재 대기열: {_message_queue.qsize()} / {config.MAX_QUEUE_SIZE} ({usage:.0f}%)\n\n"
            "이 메시지들은 검사 자체가 되지 않았습니다. 트래픽이 계속 많다면 "
            "`config.MAX_CONCURRENT_AI_CALLS`(동시 AI 호출 수)나 `MAX_QUEUE_SIZE` 상향을 검토하세요."
        ),
        color=discord.Color.red(),
        timestamp=now,
    )
    await send_log(guild, embed, mention=config.ADMIN_REVIEW_MENTION or None)


_BARTER_EXTERNAL_CONTACT_PATTERN = re.compile(
    r"(?<![a-z])(?:dm|pm)(?![a-z])|디\s*엠|개인\s*(?:메시지|연락|톡)|쪽지|"
    r"카(?:카오)?톡|오픈\s*채팅|텔레그램|계좌|예금주|입금|송금|문화\s*상품권|페이팔|paypal",
    re.IGNORECASE,
)


def _normalize_channel_name(name: str) -> str:
    return "".join(ch for ch in name if ch not in "-_ ").casefold()


def _is_barter_channel(channel) -> bool:
    """물물교환 포럼 자체와 그 아래의 각 거래 스레드를 함께 인식한다."""
    if channel is None:
        return False
    parent = getattr(channel, "parent", None)
    ids = {
        getattr(channel, "id", None),
        getattr(channel, "parent_id", None),
        getattr(parent, "id", None),
    }
    if any(channel_id in config.BARTER_CHANNEL_IDS for channel_id in ids if channel_id):
        return True
    configured_names = {
        _normalize_channel_name(name) for name in config.BARTER_CHANNEL_NAMES
    }
    return any(
        _normalize_channel_name(name) in configured_names
        for name in (getattr(channel, "name", None), getattr(parent, "name", None))
        if name
    )


def _ensure_barter_contact_check(message: discord.Message, result: FilterResult) -> FilterResult:
    """짧은 '디엠' 같은 외부 거래 신호가 길이 필터로 무검사 통과하지 않게 한다."""
    if (result.decision == "SKIP" and _is_barter_channel(message.channel)
            and _BARTER_EXTERNAL_CONTACT_PATTERN.search(message.content or "")):
        return FilterResult("NEEDS_AI")
    return result


async def _barter_conversation_context(message: discord.Message) -> list[dict]:
    """
    물물교환 거래 글의 앞선 대화를 비식별 화자 표기로 수집한다.

    최근 메시지부터 글자 예산을 채워 장기 스레드도 비용을 제한하며, Discord 조회에
    실패하면 현재 메시지만으로 보수적으로 판단하도록 빈 문맥을 반환한다.
    """
    if not _is_barter_channel(message.channel):
        return []
    history = getattr(message.channel, "history", None)
    if history is None:
        return []

    collected = []
    remaining_chars = config.BARTER_CONTEXT_MAX_CHARS
    starter_entry = None
    starter_id = None

    # 포럼 글/스레드는 첫 게시물이 거래 조건의 핵심인 경우가 많다. 대화가 길어져 최근
    # 200개 밖으로 밀려도 시작 글은 별도로 확보해 전체 거래 방식 판단에서 빠지지 않게 한다.
    if getattr(message.channel, "parent", None) is not None:
        fetch_message = getattr(message.channel, "fetch_message", None)
        if fetch_message is not None and getattr(message.channel, "id", None) != message.id:
            try:
                starter = await fetch_message(message.channel.id)
                starter_content = (getattr(starter, "content", "") or "").strip()
                if starter_content and not getattr(getattr(starter, "author", None), "bot", False):
                    starter_content = starter_content[:min(2000, remaining_chars)]
                    starter_id = getattr(starter, "id", None)
                    starter_entry = (
                        getattr(getattr(starter, "author", None), "id", None),
                        starter_content,
                    )
                    remaining_chars -= len(starter_content)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    try:
        async for prior in history(
                limit=config.BARTER_CONTEXT_MESSAGE_LIMIT,
                before=message,
                oldest_first=False):
            if remaining_chars <= 0:
                break
            if getattr(getattr(prior, "author", None), "bot", False):
                continue
            if starter_id is not None and getattr(prior, "id", None) == starter_id:
                continue
            content = (getattr(prior, "content", "") or "").strip()
            if not content:
                continue
            if len(content) > remaining_chars:
                content = (
                    "…" if remaining_chars == 1
                    else "…" + content[-(remaining_chars - 1):]
                )
            collected.append((getattr(getattr(prior, "author", None), "id", None), content))
            remaining_chars -= len(content)
            if remaining_chars <= 0:
                break
    except (discord.Forbidden, discord.HTTPException):
        return []

    collected.reverse()
    if starter_entry is not None:
        collected.insert(0, starter_entry)
    current_author_id = getattr(message.author, "id", None)
    other_authors = {}
    context = []
    for author_id, content in collected:
        if author_id is not None and author_id == current_author_id:
            speaker = "current_user"
        else:
            author_key = author_id if author_id is not None else f"unknown_{len(other_authors)}"
            if author_key not in other_authors:
                other_authors[author_key] = f"other_user_{len(other_authors) + 1}"
            speaker = other_authors[author_key]
        context.append({"speaker": speaker, "content": content})
    return context


async def _fast_check_with_invite_context(message: discord.Message) -> FilterResult:
    """Allow only verified same-guild voice invites from configured source channels."""
    invite_urls = extract_discord_invite_urls(message.content)
    if not invite_urls or not config.BLOCK_DISCORD_INVITES:
        result = fast_check(message.guild.id, message.author.id, message.content)
        return _ensure_barter_contact_check(message, result)

    if len(invite_urls) > 3:
        return FilterResult("DECIDED", "MODERATE", "한 메시지에 디스코드 초대 링크 과다 게시")

    if message.channel.id not in config.INTERNAL_VOICE_INVITE_SOURCE_CHANNEL_IDS:
        return FilterResult("DECIDED", "MODERATE", "허용되지 않은 채널의 디스코드 초대 링크")

    for url in invite_urls:
        try:
            invite = await bot.fetch_invite(url, with_counts=False, with_expiration=False)
        except discord.NotFound:
            # 만료·삭제된 초대는 실제로 다른 서버에 들어갈 수 없으므로 링크만으로 제재하지 않는다.
            continue
        except (discord.Forbidden, discord.HTTPException) as error:
            # Discord 장애/한도 때문에 내부 링크를 외부 홍보로 오판하지 않도록 fail-open한다.
            print(f"[invite-check] 초대 대상 확인 실패로 링크 제재 보류: {type(error).__name__}")
            continue

        target_guild_id = getattr(getattr(invite, "guild", None), "id", None)
        target_type = getattr(getattr(invite, "channel", None), "type", None)
        if target_guild_id != message.guild.id:
            return FilterResult("DECIDED", "MODERATE", "다른 디스코드 서버 초대 링크 무단 게시")
        if target_type not in (discord.ChannelType.voice, discord.ChannelType.stage_voice):
            return FilterResult("DECIDED", "MODERATE", "허용 채널에는 같은 서버 음성채널 초대만 허용")

    # 링크 자체는 검증됐지만 함께 적힌 욕설·광고 등은 기존 필터와 AI가 계속 검사한다.
    result = fast_check(
        message.guild.id, message.author.id, message.content,
        allow_discord_invites=True,
    )
    return _ensure_barter_contact_check(message, result)


async def _run_moderation(message: discord.Message):
    """1차 필터 → (필요 시) AI 큐 투입. 새 메시지와 수정된 메시지가 같은 경로를 탄다."""
    processing_key = _message_processing_key(message)
    if processing_key in _processing_keys:
        return

    _record_split_message(message)

    result = await _fast_check_with_invite_context(message)
    image_needs_ai = vision.has_image_attachments(message)

    # 욕설 키워드 한 조각만 보고 즉시 확정하면 "시발" + "점이 어디예요?" 같은 한국어
    # 분할 발화를 오판한다. 초대 링크·도배처럼 문맥과 무관한 확정 규칙은 그대로 유지하고,
    # 금칙어 결정만 짧게 뒤 문장을 기다린 뒤 AI가 앞뒤 조각과 함께 판단하게 한다.
    keyword_needs_context = (
        config.SPLIT_MESSAGE_CONTEXT_ENABLED
        and (
            (result.decision == "DECIDED" and "금칙어" in (result.reason or ""))
            or (result.decision == "SKIP" and _split_candidate_contains_keyword(message))
        )
    )
    # 개별 조각은 정상 필터를 통과했더라도 같은 작성자가 12초 안에 이어 보냈다면
    # 최신 조각을 AI에 올려 전체 발화로 재구성한다. 다른 사용자의 메시지는 결합하지 않는다.
    split_sequence_needs_ai = (
        config.SPLIT_MESSAGE_CONTEXT_ENABLED
        and result.decision == "SKIP"
        and _has_recent_same_author_fragment(message)
    )
    # "네", "맞음", "아닌데"처럼 단독으로는 정상인 짧은 답글도 원문에 따라 의미가
    # 달라지므로, 답글 원문이 있으면 AI 문맥 판단 대상으로 올린다.
    reply_needs_ai = result.decision == "SKIP" and _reply_parent_id(message) is not None

    if (result.decision == "SKIP" and not keyword_needs_context
            and not split_sequence_needs_ai and not reply_needs_ai and not image_needs_ai):
        pass  # 정상 메시지, 아무 조치 없음
    elif result.decision == "DECIDED" and not keyword_needs_context and not image_needs_ai:
        # 이벤트 핸들러를 막지 않도록 별도 태스크로 처리 (도배 레이드 시 지연 방지)
        _processing_keys.add(processing_key)
        _spawn(_handle_decided(message, result, processing_key))
    else:  # NEEDS_AI -> 큐에 넣어 워커가 비동기 처리 (여기서 기다리지 않음)
        _processing_keys.add(processing_key)
        try:
            _message_queue.put_nowait((
                message, time.monotonic(), processing_key,
                bool(
                    config.SPLIT_MESSAGE_CONTEXT_ENABLED
                    and (message.content or "").strip()
                    and (keyword_needs_context or split_sequence_needs_ai or reply_needs_ai
                         or result.decision == "NEEDS_AI")
                ),
            ))
        except asyncio.QueueFull:
            _processing_keys.discard(processing_key)
            _note_drop(message.guild)
            if _dropped_count % 100 == 1:
                print(f"⚠️ 처리 큐가 가득 차 메시지를 버렸습니다 (누적 {_dropped_count}건). "
                      f"MAX_QUEUE_SIZE 또는 MAX_CONCURRENT_AI_CALLS 조정을 고려하세요.")


@bot.event
async def on_message(message: discord.Message):
    if not _is_moderation_target(message):
        # 관리자 메시지/명령어일 수 있으므로 명령어 처리는 계속 진행
        if not message.author.bot and message.guild is not None:
            await bot.process_commands(message)
        return

    await _run_moderation(message)
    # 명령어는 위에서 조기 처리되므로 여기서는 process_commands를 다시 호출하지 않는다


async def _recent_timeout_audit_context(guild: discord.Guild, user_id: int):
    """Discord에서 직접 변경한 타임아웃의 처리자와 감사 사유를 가능한 범위에서 찾는다."""
    try:
        async for entry in guild.audit_logs(
                limit=10, action=discord.AuditLogAction.member_update):
            target_id = getattr(getattr(entry, "target", None), "id", None)
            age = (discord.utils.utcnow() - entry.created_at).total_seconds()
            if target_id == user_id and 0 <= age <= 30:
                actor = getattr(entry, "user", None)
                return actor, (entry.reason or "Discord에서 관리자가 직접 변경")
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None, "Discord에서 관리자가 직접 변경 (감사 로그 처리자 확인 불가)"


def _audit_target_display(guild: discord.Guild, target, target_id: int) -> str:
    """감사 로그 대상이 캐시에 없어 Object로 와도 안정적인 사용자 표기를 만든다."""
    member = guild.get_member(target_id) if hasattr(guild, "get_member") else None
    if member is not None:
        return _member_ledger_display(member, target_id)
    name = getattr(target, "display_name", None) or getattr(target, "name", None)
    account = str(target) if target is not None else ""
    if name:
        return f"{name} ({account})" if account and account != name else str(name)
    if account and not account.startswith("<Object "):
        return account
    return f"Discord 사용자 {target_id}"


def _audit_actor_context(entry) -> tuple[int | None, str | None, str]:
    actor = getattr(entry, "user", None)
    actor_id = getattr(entry, "user_id", None) or getattr(actor, "id", None)
    actor_display = _member_ledger_display(actor, actor_id) if actor_id else None
    actor_kind = "외부 봇" if getattr(actor, "bot", False) else "관리자"
    return actor_id, actor_display, actor_kind


def _audit_timeout_timestamp(value) -> float:
    return value.timestamp() if value is not None and hasattr(value, "timestamp") else 0.0


async def _process_external_sanction_audit_entry(entry) -> bool:
    """관리자·다른 봇의 Discord 기본 제재 감사 항목을 원장에 반영한다."""
    action = getattr(entry, "action", None)
    supported = {
        discord.AuditLogAction.ban,
        discord.AuditLogAction.unban,
        discord.AuditLogAction.kick,
        discord.AuditLogAction.message_delete,
        discord.AuditLogAction.message_bulk_delete,
        discord.AuditLogAction.member_update,
    }
    if action not in supported:
        return False

    guild = getattr(entry, "guild", None)
    target = getattr(entry, "target", None)
    target_id = getattr(target, "id", None)
    if guild is None or target_id is None:
        return False

    actor_id, actor_display, actor_kind = _audit_actor_context(entry)
    own_user_id = getattr(getattr(bot, "user", None), "id", None)
    if own_user_id is not None and actor_id == own_user_id:
        # BB봇이 직접 실행한 조치는 검수/명령 처리 시 이미 원장에 기록한다.
        return False

    entry_id = int(getattr(entry, "id"))
    issued_at = getattr(entry, "created_at", None)
    issued_ts = issued_at.timestamp() if issued_at is not None else time.time()
    display = _audit_target_display(guild, target, int(target_id))
    reason = getattr(entry, "reason", None)

    if action == discord.AuditLogAction.ban:
        await database.record_sanction(
            guild.id, target_id, display, "BAN",
            reason or f"{actor_kind}가 Discord에서 차단 (사유 미입력)",
            "discord_audit", f"discord-ban:{target_id}:{entry_id}",
            issued_by_id=actor_id, issued_by_display=actor_display,
            issued_at=issued_ts,
        )
        return True

    if action == discord.AuditLogAction.unban:
        await database.release_active_sanctions(
            guild.id, target_id, "BAN",
            reason or f"{actor_kind}가 Discord에서 차단 해제 (사유 미입력)",
            released_by_id=actor_id, released_by_display=actor_display,
            released_at=issued_ts,
        )
        return True

    if action == discord.AuditLogAction.kick:
        await database.record_sanction(
            guild.id, target_id, display, "KICK",
            reason or f"{actor_kind}가 Discord에서 추방 (사유 미입력)",
            "discord_audit", f"discord-kick:{target_id}:{entry_id}",
            issued_by_id=actor_id, issued_by_display=actor_display,
            issued_at=issued_ts,
        )
        return True

    if action in {
        discord.AuditLogAction.message_delete,
        discord.AuditLogAction.message_bulk_delete,
    }:
        extra = getattr(entry, "extra", None)
        count = max(1, int(getattr(extra, "count", 1) or 1))
        channel = getattr(extra, "channel", None)
        channel_label = getattr(channel, "mention", None) or (
            f"#{channel.name}" if getattr(channel, "name", None) else "채널 미확인"
        )
        detail = f"메시지 {count:,}건 삭제 · {channel_label}"
        await database.record_sanction(
            guild.id, target_id, display, "DELETE",
            f"{reason} ({detail})" if reason else f"{actor_kind} 조치: {detail}",
            "discord_audit", f"discord-delete:{target_id}:{entry_id}",
            issued_by_id=actor_id, issued_by_display=actor_display,
            issued_at=issued_ts,
        )
        return True

    before_until = getattr(getattr(entry, "before", None), "timed_out_until", None)
    after_until = getattr(getattr(entry, "after", None), "timed_out_until", None)
    if before_until is None and after_until is None:
        return False
    before_ts = _audit_timeout_timestamp(before_until)
    after_ts = _audit_timeout_timestamp(after_until)
    if after_ts > issued_ts:
        if before_ts > issued_ts:
            await database.release_active_sanctions(
                guild.id, target_id, "TIMEOUT", "타임아웃 기간 변경",
                released_by_id=actor_id, released_by_display=actor_display,
                released_at=issued_ts,
            )
        await database.record_sanction(
            guild.id, target_id, display, "TIMEOUT",
            reason or f"{actor_kind}가 Discord에서 타임아웃 적용 (사유 미입력)",
            "discord_audit", f"discord-timeout:{target_id}:{int(after_ts)}",
            issued_by_id=actor_id, issued_by_display=actor_display,
            issued_at=issued_ts, expires_at=after_ts,
        )
    elif before_ts > 0:
        await database.release_active_sanctions(
            guild.id, target_id, "TIMEOUT",
            reason or f"{actor_kind}가 Discord에서 타임아웃 해제 (사유 미입력)",
            released_by_id=actor_id, released_by_display=actor_display,
            released_at=issued_ts,
        )
    return True


@bot.event
async def on_audit_log_entry_create(entry: discord.AuditLogEntry):
    """실시간 감사 로그로 관리자·외부 봇의 Discord 기본 제재를 수집한다."""
    await _process_external_sanction_audit_entry(entry)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Discord UI에서 직접 적용·해제한 타임아웃도 인수인계 원장에 남긴다."""
    before_until = getattr(before, "timed_out_until", None)
    after_until = getattr(after, "timed_out_until", None)
    before_ts = before_until.timestamp() if before_until else 0.0
    after_ts = after_until.timestamp() if after_until else 0.0
    if abs(before_ts - after_ts) <= 1:
        return
    if _consume_bot_timeout_change(after.guild.id, after.id, after_ts or None):
        return

    actor, audit_reason = await _recent_timeout_audit_context(after.guild, after.id)
    actor_id = getattr(actor, "id", None)
    actor_display = _member_ledger_display(actor, actor_id) if actor_id else None
    now = time.time()
    if after_ts > now:
        if before_ts > now:
            await database.release_active_sanctions(
                after.guild.id, after.id, "TIMEOUT", "타임아웃 기간 변경",
                released_by_id=actor_id, released_by_display=actor_display,
            )
        await database.record_sanction(
            after.guild.id, after.id, _member_ledger_display(after, after.id),
            "TIMEOUT", audit_reason, "discord_manual",
            f"discord-timeout:{after.id}:{int(after_ts)}",
            issued_by_id=actor_id, issued_by_display=actor_display,
            expires_at=after_ts,
        )
    elif before_ts > 0:
        naturally_expired = before_ts <= now + 5 and actor is None
        await database.release_active_sanctions(
            after.guild.id, after.id, "TIMEOUT",
            "설정된 타임아웃 기간 만료" if naturally_expired else audit_reason,
            released_by_id=actor_id, released_by_display=actor_display,
            status="expired" if naturally_expired else "released",
        )


async def _recent_ban_audit_context(guild: discord.Guild, user_id: int, *, unban: bool):
    """Discord에서 직접 실행한 밴·밴 해제의 처리자, 사유와 감사 항목 ID를 찾는다."""
    action = discord.AuditLogAction.unban if unban else discord.AuditLogAction.ban
    fallback = "Discord에서 관리자가 직접 밴 해제" if unban else "Discord에서 관리자가 직접 밴"
    # 멤버 이벤트가 감사 로그 생성보다 조금 먼저 도착할 수 있어 짧게 두 번 더 확인한다.
    for attempt in range(3):
        try:
            async for entry in guild.audit_logs(limit=10, action=action):
                target_id = getattr(getattr(entry, "target", None), "id", None)
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                if target_id == user_id and 0 <= age <= 30:
                    actor = getattr(entry, "user", None)
                    return actor, (entry.reason or fallback), getattr(entry, "id", None)
        except (discord.Forbidden, discord.HTTPException):
            break
        if attempt < 2:
            await asyncio.sleep(0.5)
    return None, f"{fallback} (감사 로그 처리자 확인 불가)", None


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User):
    """Discord UI에서 직접 적용한 밴도 처리자와 함께 인수인계 원장에 기록한다."""
    if _consume_bot_ban(guild.id, user.id):
        return
    actor, audit_reason, audit_id = await _recent_ban_audit_context(
        guild, user.id, unban=False
    )
    actor_id = getattr(actor, "id", None)
    await database.record_sanction(
        guild.id, user.id, _member_ledger_display(user, user.id),
        "BAN", audit_reason, "discord_manual",
        f"discord-ban:{user.id}:{audit_id or int(time.time())}",
        issued_by_id=actor_id,
        issued_by_display=_member_ledger_display(actor, actor_id) if actor_id else None,
    )


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User):
    """Discord UI에서 직접 해제한 밴의 처리자와 사유를 기존 원장에 반영한다."""
    actor, audit_reason, _audit_id = await _recent_ban_audit_context(
        guild, user.id, unban=True
    )
    actor_id = getattr(actor, "id", None)
    await database.release_active_sanctions(
        guild.id, user.id, "BAN", audit_reason,
        released_by_id=actor_id,
        released_by_display=_member_ledger_display(actor, actor_id) if actor_id else None,
    )


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    """
    수정된 메시지도 새 메시지와 동일하게 검사한다.
    (깨끗한 메시지를 올린 뒤 욕설로 수정하는 우회 방지)
    raw 이벤트를 쓰는 이유: 기본 on_message_edit은 봇 캐시에 있는 최근 메시지만 잡는데,
    raw는 오래된 메시지를 수정해도 잡힌다.
    """
    # 링크 미리보기(임베드) 생성/고정 등 본문·첨부가 안 바뀐 수정 이벤트는 무시한다.
    # 핵의심 신고는 첨부 교체/삭제도 판단 근거가 달라지는 수정이므로 attachments를 함께 본다.
    if "content" not in payload.data and "attachments" not in payload.data:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None or not hasattr(channel, "fetch_message"):
        return
    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return

    if not _is_moderation_target(message):
        return
    if not message.content.strip() and not vision.has_image_attachments(message):
        return
    if (payload.cached_message is not None
            and payload.cached_message.content == message.content
            and vision.attachment_fingerprint(payload.cached_message)
            == vision.attachment_fingerprint(message)):
        return
    await _run_moderation(message)


# ── 관리자용 명령어 ──────────────────────────────────────────────────

@bot.command(name="명령어", aliases=["도움말", "help"])
async def show_commands(ctx):
    """전체 명령어 목록을 보여준다 (모든 명령어는 관리자 전용)."""
    embed = discord.Embed(
        title="📖 Big Brother 명령어 목록",
        description="모든 명령어는 **관리자 전용**입니다.\n"
                    "접두사는 `!BB` 입니다. `!BB 점수`처럼 띄어 써도, `!BB점수`처럼 붙여 써도 되고 "
                    "대소문자(`!bb`)도 구분하지 않아요.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🔍 조회",
        value=(
            "`!BB 명령어` — 이 목록 (별칭: 도움말, help)\n"
            "`!BB 규칙확인` — 현재 설정된 서버 규칙 확인\n"
            "`!BB 점수 @유저` — 누적 위반 점수와 최근 이력 확인\n"
            "`!BB 제재내역 [@유저]` — 경고·타임아웃 적용/해제 인수인계 기록\n"
            "`!BB 인수인계` — 비밀번호 보호 운영 대시보드 주소를 DM으로 받기\n"
            "`!BB 검토대기 [시간]` — 킥/밴 대신 하향 조정된 건 목록 (기본 72시간)\n"
            "`!BB 오탐학습 [개수]` — 활성 오탐 학습 규칙과 적용 범위 조회\n"
            "`!BB KPI [월|분기|연]` — 현재 카드 정탐·오탐 운영 지표 조회\n"
            "`!BB 상태` — 처리 대기열/워커/드롭 건수 확인"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ 실행",
        value=(
            "`!BB 점수초기화 @유저` — 유저의 누적 점수 초기화\n"
            "`!BB 경고등록 @유저 <사유>` — 수동 경고를 원장에 기록\n"
            "`!BB 경고해제 @유저 <사유>` — 최근 활성 경고 해제 기록\n"
            "`!BB 타임아웃등록 @유저 <분> <사유>` — 타임아웃 적용과 원장 기록\n"
            "`!BB 타임아웃해제 @유저 <사유>` — 타임아웃 해제와 원장 기록\n"
            "`!BB 오탐취소 <규칙번호>` — 잘못 등록한 오탐 학습 규칙 취소\n"
            "`!BB 검토복구 <번호>` — 중단된 검수를 확인 후 다시 대기 상태로 전환\n"
            "`!BB 감사실행 [backend]` — 배치 감사를 지금 바로 실행 (gemini/groq/ollama/auto)"
        ),
        inline=False,
    )
    embed.set_footer(text="위반 검수는 #제재-로그 채널의 카드 버튼으로 처리합니다.")
    await ctx.send(embed=embed)


@bot.command(name="점수")
@commands.has_permissions(administrator=True)
async def check_points(ctx, member: discord.Member):
    points = await database.get_points(ctx.guild.id, member.id)
    history = await database.get_recent_violations(ctx.guild.id, member.id, limit=5)

    embed = discord.Embed(title=f"{member.display_name}의 위반 현황", color=discord.Color.blue())
    embed.add_field(name="누적 점수", value=f"{points:.1f}", inline=False)
    if history:
        lines = [f"- [{lvl}] {reason} → {action} ({provider})" for lvl, reason, action, provider, _ in history]
        embed.add_field(name="최근 위반 이력", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="최근 위반 이력", value="없음", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="점수초기화")
@commands.has_permissions(administrator=True)
async def reset_points_cmd(ctx, member: discord.Member):
    await database.reset_points(ctx.guild.id, member.id)
    await ctx.send(f"✅ {member.mention}의 위반 점수를 초기화했습니다.")


@bot.command(name="경고등록")
@commands.has_permissions(administrator=True)
async def register_warning_cmd(ctx, member: discord.Member, *, reason: str):
    sanction_id = await database.record_sanction(
        ctx.guild.id, member.id, _member_ledger_display(member, member.id),
        "WARNING", reason, "manual_command", f"manual-warning:{ctx.message.id}",
        issued_by_id=ctx.author.id,
        issued_by_display=_member_ledger_display(ctx.author, ctx.author.id),
    )
    await ctx.send(
        f"✅ 경고 기록 `#{sanction_id}`을 인수인계 원장에 등록했습니다. "
        "사용자에게 별도 메시지는 전송하지 않았습니다."
    )


@bot.command(name="경고해제")
@commands.has_permissions(administrator=True)
async def release_warning_cmd(ctx, member: discord.Member, *, reason: str):
    ids = await database.release_active_sanctions(
        ctx.guild.id, member.id, "WARNING", reason,
        released_by_id=ctx.author.id,
        released_by_display=_member_ledger_display(ctx.author, ctx.author.id), limit=1,
    )
    if not ids:
        await ctx.send("⚠️ 해당 사용자의 활성 경고 기록을 찾지 못했습니다.")
        return
    await ctx.send(f"✅ 경고 기록 `#{ids[0]}`을 해제 처리했습니다.")


@bot.command(name="타임아웃등록")
@commands.has_permissions(administrator=True)
async def register_timeout_cmd(ctx, member: discord.Member, minutes: int, *, reason: str):
    if minutes < 1 or minutes > 40320:
        await ctx.send("타임아웃 시간은 1분 이상 40,320분(28일) 이하여야 합니다.")
        return
    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    _remember_bot_timeout_change(ctx.guild.id, member.id, until.timestamp())
    try:
        await member.timeout(until, reason=f"{ctx.author}: {reason}"[:480])
    except (discord.Forbidden, discord.HTTPException):
        _pending_bot_timeout_changes.pop((int(ctx.guild.id), int(member.id)), None)
        await ctx.send("⛔ 타임아웃 적용에 실패했습니다. 봇과 대상 사용자의 역할 순서를 확인하세요.")
        return
    sanction_id = await database.record_sanction(
        ctx.guild.id, member.id, _member_ledger_display(member, member.id),
        "TIMEOUT", reason, "manual_command", f"manual-timeout:{ctx.message.id}",
        issued_by_id=ctx.author.id,
        issued_by_display=_member_ledger_display(ctx.author, ctx.author.id),
        expires_at=until.timestamp(),
    )
    await ctx.send(
        f"✅ {minutes:,}분 타임아웃을 적용하고 기록 `#{sanction_id}`을 원장에 등록했습니다."
    )


@bot.command(name="타임아웃해제")
@commands.has_permissions(administrator=True)
async def release_timeout_cmd(ctx, member: discord.Member, *, reason: str):
    _remember_bot_timeout_change(ctx.guild.id, member.id, None)
    try:
        await member.timeout(None, reason=f"{ctx.author}: {reason}"[:480])
    except (discord.Forbidden, discord.HTTPException):
        _pending_bot_timeout_changes.pop((int(ctx.guild.id), int(member.id)), None)
        await ctx.send("⛔ 타임아웃 해제에 실패했습니다. 봇과 대상 사용자의 역할 순서를 확인하세요.")
        return
    ids = await database.release_active_sanctions(
        ctx.guild.id, member.id, "TIMEOUT", reason,
        released_by_id=ctx.author.id,
        released_by_display=_member_ledger_display(ctx.author, ctx.author.id),
    )
    await ctx.send(
        f"✅ 타임아웃을 해제했습니다. 원장 기록 {len(ids)}건을 해제 상태로 변경했습니다."
    )


@bot.command(name="제재내역")
@commands.has_permissions(administrator=True)
async def sanction_history_cmd(ctx, member: discord.Member | None = None):
    rows = await database.get_sanction_history(
        ctx.guild.id, member.id if member else None, limit=15,
    )
    if not rows:
        await ctx.send("등록된 경고·타임아웃 인수인계 기록이 없습니다.")
        return
    lines = []
    for row in rows:
        issued = f"<t:{int(row['issued_at'])}:f>"
        released = (
            f"<t:{int(row['released_at'])}:f>"
            if row["released_at"] else "현재 활성"
        )
        released_actor = (
            _sanction_actor_text(row["released_by_display"], row["released_by_id"])
            if row["released_at"] else "아직 해제되지 않음"
        )
        lines.append(
            f"`#{row['sanction_id']}` **{row['user_display']}** · {row['action_type']} · "
            f"{row['status']}\n{issued} → {released}\n"
            f"적용 관리자: {_sanction_actor_text(row['issued_by_display'], row['issued_by_id'])}\n"
            f"해제 관리자: {released_actor}\n"
            f"사유: {row['reason'][:180]}"
        )
    embed = discord.Embed(
        title="🔒 스태프 제재 인수인계 기록",
        description="\n\n".join(lines)[:4000],
        color=discord.Color.dark_blue(),
    )
    try:
        await ctx.author.send(embed=embed)
        await ctx.send("✅ 제재 인수인계 기록을 관리자 DM으로 보냈습니다.")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send("⚠️ 관리자 DM을 열 수 없어 기록을 전달하지 못했습니다.")


@bot.command(name="인수인계")
@commands.has_permissions(administrator=True)
async def handoff_dashboard_cmd(ctx):
    if not KPI_DASHBOARD_PUBLIC_URL:
        await ctx.send("⚠️ 운영 대시보드 주소가 설정되어 있지 않습니다.")
        return
    staff_url = KPI_DASHBOARD_PUBLIC_URL.rstrip("/") + "/staff"
    try:
        await ctx.author.send(
            "🔒 BB봇 스태프 인수인계 대시보드\n"
            f"{staff_url}\n\n사이트 비밀번호는 서버 책임자에게 별도로 확인해주세요."
        )
        await ctx.send("✅ 비밀번호 보호 인수인계 대시보드 주소를 관리자 DM으로 보냈습니다.")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send("⚠️ 관리자 DM을 열 수 없어 대시보드 주소를 전달하지 못했습니다.")


@bot.command(name="규칙확인")
async def show_rules(ctx):
    embed = discord.Embed(title="📜 서버 규칙", description=config.SERVER_RULES, color=discord.Color.green())
    await ctx.send(embed=embed)


@bot.command(name="검토대기")
@commands.has_permissions(administrator=True)
async def show_pending_reviews(ctx, hours: int = 72):
    """
    관리자 검토가 필요한 두 종류를 함께 보여준다:
    1. 정책상 킥/밴이 자동 실행되지 않고 타임아웃으로 하향된 건
    2. 검수 카드가 없는 대기 건 (카드 전송 실패 또는 배치 카드 상한 초과) — 카드가 없어
       버튼으로 처리할 수 없으므로, 여기서 드러나지 않으면 그대로 묻힌다.
    """
    rows = await database.get_pending_reviews(ctx.guild.id, hours=hours)
    uncarded = await database.get_reviews_without_card(ctx.guild.id)
    stalled = await database.get_stale_processing_reviews(ctx.guild.id)

    if not rows and not uncarded and not stalled:
        await ctx.send(f"✅ 최근 {hours}시간 내 관리자 검토가 필요한 건이 없습니다.")
        return

    provider_label = {"gemini": "Gemini", "groq": "Groq", "ollama": "Ollama(로컬)",
                      "filter": "키워드 필터", "none": "판단 실패"}
    embed = discord.Embed(
        title=f"⚠️ 관리자 검토 필요 목록 (최근 {hours}시간)",
        description="정책상 킥/밴은 자동 실행하지 않고 타임아웃으로 대체 적용된 건들입니다. 직접 확인 후 필요시 수동으로 킥/밴 처리하세요.",
        color=discord.Color.gold(),
    )
    for user_id, level, reason, action, provider, created_at in rows[:10]:
        member = ctx.guild.get_member(user_id)
        who = member.mention if member else f"(ID: {user_id})"
        embed.add_field(
            name=f"{who} — {level} ({provider_label.get(provider, provider)})",
            value=f"{reason}\n적용된 조치: {action}",
            inline=False,
        )

    if uncarded:
        lines = []
        for review_id, user_id, channel_id, level, reason, _action, _created_at in uncarded[:5]:
            member = ctx.guild.get_member(user_id)
            who = member.mention if member else f"(ID: {user_id})"
            lines.append(f"`#{review_id}` {who} · {level} · <#{channel_id}>\n> {(reason or '-')[:100]}")
        embed.add_field(
            name=f"🃏 카드 없는 대기 건 {len(uncarded)}개 (버튼 처리 불가)",
            value=("검수 카드가 게시되지 못한 건들입니다. 로그 채널 권한/설정을 확인하거나 "
                   "배치 카드 상한(BATCH_REVIEW_CARD_LIMIT)을 조정하세요.\n\n"
                   + "\n".join(lines))[:1024],
            inline=False,
        )
    if stalled:
        lines = []
        for review_id, user_id, channel_id, level, action, _started_at in stalled:
            member = ctx.guild.get_member(user_id)
            who = member.mention if member else f"(ID: {user_id})"
            lines.append(f"`#{review_id}` {who} · {level} · <#{channel_id}> · {action}")
        embed.add_field(
            name=f"⛔ 처리 중 중단 의심 {len(stalled)}건",
            value=("Discord 감사 로그와 대상 상태를 먼저 확인하세요. 제재가 적용되지 않은 것이 "
                   "확실한 건만 `!BB 검토복구 <번호>`로 복구할 수 있습니다.\n"
                   + "\n".join(lines))[:1024],
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="검토복구")
@commands.has_permissions(administrator=True)
async def recover_review_cmd(ctx, review_id: int):
    """외부 제재 미적용을 관리자가 확인한 중단 검수를 다시 누를 수 있게 한다."""
    recovered = await database.recover_processing_review(review_id, ctx.guild.id)
    if recovered:
        await ctx.send(
            f"✅ 검토 `#{review_id}`을 대기 상태로 복구했습니다. 기존 카드에서 다시 처리하세요. "
            "이미 제재가 적용된 건이었다면 중복 실행될 수 있으니 대상 상태를 반드시 확인하세요."
        )
    else:
        await ctx.send("⚠️ 해당 서버에서 10분 이상 중단된 검토 건을 찾지 못했습니다.")


@bot.command(name="감사실행")
@commands.has_permissions(administrator=True)
async def run_audit_now(ctx, backend: str = None):
    """등록된 감시 채널들을 지금 바로 감사한다. 예: !BB 감사실행 gemini"""
    backend = backend or config.BATCH_BACKEND
    if backend not in ("auto", "gemini", "groq", "ollama"):
        await ctx.send("backend은 auto/gemini/groq/ollama 중 하나여야 합니다.")
        return
    await ctx.send(f"🔍 배치 감사를 시작합니다 (backend={backend})... 채널 수/기간에 따라 시간이 걸릴 수 있어요.")
    file_path = await run_full_audit(ctx.guild, backend=backend, on_flagged=_batch_review_callback())
    if file_path:
        await ctx.send(f"✅ 감사 완료. 리포트: `{file_path}`" +
                        (f" (또한 <#{os.environ.get('REPORT_CHANNEL_ID')}>에도 전송됨)"
                         if os.environ.get("REPORT_CHANNEL_ID") else ""))
    else:
        await ctx.send("검토할 새 메시지가 없거나 감시 채널이 등록되지 않았습니다. `config.WATCHED_CHANNEL_IDS`를 확인하세요.")


def _fallback_chain_status() -> str:
    """무료 한도가 마르면 어디까지 버틸 수 있는지를 `!BB 상태`에서 한눈에 보여준다."""
    labels = {"gemini": "Gemini", "groq": "Groq", "ollama": f"Ollama(`{config.OLLAMA_MODEL}`)"}
    enabled = [provider for provider in config.REALTIME_PROVIDER_ORDER
               if provider != "ollama" or config.OLLAMA_REALTIME_FALLBACK]
    chain = " → ".join(labels[provider] for provider in enabled)
    if "ollama" not in enabled:
        return (f"{chain}\n⚠️ 클라우드 한도가 모두 소진되면 키워드 필터만 남습니다. "
                "로컬 Ollama를 순서에 넣고 OLLAMA_REALTIME_FALLBACK을 켜세요.")

    ready, cooldown = moderator.ollama_fallback_status()
    if ready:
        return f"{chain}\n🟢 로컬 제공자 사용 가능 (최근 연결 실패 없음)"
    left = f"{cooldown / 60:.0f}분" if cooldown >= 60 else f"{cooldown:.0f}초"
    return (f"{chain}\n🔴 Ollama 연결 실패로 {left}간 건너뛰는 중 — "
            f"봇이 도는 PC에서 Ollama가 실행 중인지, `{config.OLLAMA_MODEL}` 모델이 "
            "받아져 있는지 확인하세요.")


def _vision_chain_status() -> str:
    if not config.VISION_ANALYSIS_ENABLED:
        return "비활성화"
    labels = {
        "ollama": f"Ollama(`{config.OLLAMA_VISION_MODEL}`)",
        "gemini": f"Gemini(`{config.GEMINI_VISION_MODEL}`)",
        "groq": f"Groq(`{config.GROQ_VISION_MODEL}`)",
    }
    chain = " → ".join(labels[provider] for provider in config.VISION_PROVIDER_ORDER)
    cooling = [
        f"{provider} {vision.provider_cooldown_remaining(provider):.0f}초"
        for provider in config.VISION_PROVIDER_ORDER
        if vision.provider_cooldown_remaining(provider) > 0
    ]
    state = " · 회로 차단: " + ", ".join(cooling) if cooling else " · 최근 장애 없음"
    return f"{chain}{state}\n대상 채널 {len(config.VISION_CHANNEL_IDS)}개 · 이미지 최대 {config.VISION_MAX_IMAGES}장"


@bot.command(name="KPI", aliases=["kpi", "통계"])
@commands.has_permissions(administrator=True)
async def show_kpi(ctx, period_name: str = "월"):
    """현재 월·분기·연도의 관리자 확정 카드 KPI를 즉시 보여준다."""
    kind_by_name = {
        "월": "month", "월간": "month", "month": "month",
        "분기": "quarter", "분기간": "quarter", "quarter": "quarter",
        "연": "year", "연간": "year", "년": "year", "year": "year",
    }
    kind = kind_by_name.get(period_name.casefold())
    if kind is None:
        await ctx.send("사용법: `!BB KPI [월|분기|연]`")
        return
    period = kpi.current_period(kind)
    summary = await kpi.get_period_summary(
        int(ctx.guild.id), period, _kpi_channel_labels(ctx.guild)
    )
    await ctx.send(embed=_kpi_report_embed(summary))


@bot.command(name="상태")
@commands.has_permissions(administrator=True)
async def show_status(ctx):
    """대기열/워커 상태를 확인 (트래픽이 많을 때 큐가 밀리는지 점검용)."""
    embed = discord.Embed(title="⚙️ 자동 제재 봇 상태", color=discord.Color.blurple())
    embed.add_field(
        name="운영 모드",
        value="🔍 수동 검수 (감지만 하고 조치 없음)" if config.MANUAL_REVIEW_MODE else "🚨 자동 조치",
        inline=False,
    )
    embed.add_field(
        name="사용자 제재 메시지",
        value=("DM 전송" if config.USER_SANCTION_DM_ENABLED else "전송 안 함")
              + (" · 공개 제재 로그 사용" if config.PUBLIC_SANCTION_LOG_ENABLED
                 else " · 공개 제재 로그 사용 안 함"),
        inline=False,
    )
    queued = _message_queue.qsize()
    usage = queued / config.MAX_QUEUE_SIZE * 100
    # 큐 사용률이 높다는 건 곧 드롭(감시 구멍)이 시작된다는 신호다.
    gauge = "🟢 여유" if usage < 80 else ("🟡 혼잡" if usage < 95 else "🔴 포화 임박")
    embed.add_field(name="대기열", value=f"{gauge} {queued} / {config.MAX_QUEUE_SIZE} ({usage:.0f}%)", inline=True)
    embed.add_field(name="AI 워커 수", value=str(config.MAX_CONCURRENT_AI_CALLS), inline=True)
    embed.add_field(name="판단 폴백 사슬", value=_fallback_chain_status(), inline=False)
    embed.add_field(name="이미지 OCR·비전 분석", value=_vision_chain_status(), inline=False)
    retry_count = await database.count_moderation_retries(ctx.guild.id)
    embed.add_field(
        name="AI 장애 재검사 대기",
        value=(f"{retry_count}건" if retry_count else "없음"),
        inline=True,
    )
    kpi_pending = await database.count_kpi_sync_pending(int(ctx.guild.id))
    dashboard_state = (
        f"연결됨 · 전송 대기 {kpi_pending}건"
        if KPI_DASHBOARD_INGEST_URL and KPI_DASHBOARD_INGEST_TOKEN
        else f"사이트 미연결 · 로컬 보관 {kpi_pending}건"
    )
    embed.add_field(name="KPI 대시보드", value=dashboard_state, inline=True)

    total_dropped = _dropped_count + _expired_count
    drop_text = (f"큐 포화 {_dropped_count}건 · 대기 초과 {_expired_count}건"
                 if total_dropped else "없음")
    embed.add_field(name=f"검사 못한 메시지 (누적 {total_dropped}건)", value=drop_text, inline=False)
    if _last_drop_at:
        embed.add_field(name="마지막 누락 시각",
                        value=discord.utils.format_dt(_last_drop_at, "R"), inline=True)

    uncarded = await database.get_reviews_without_card(ctx.guild.id)
    if uncarded:
        embed.add_field(name="카드 없는 검수 대기",
                        value=f"{len(uncarded)}건 — `!BB 검토대기`에서 확인", inline=True)
    outage = _ai_outage_state.get(ctx.guild.id, {})
    if outage.get("streak"):
        embed.add_field(name="AI 연속 판단 실패", value=f"{outage['streak']}회", inline=True)
    label_stats = await database.get_moderation_label_stats(ctx.guild.id)
    if label_stats:
        language_names = {
            "ko": "한국어", "ja": "일본어", "zh": "중국어", "latin": "라틴 문자",
            "cyrillic": "키릴 문자", "arabic": "아랍 문자", "devanagari": "데바나가리",
            "thai": "태국 문자", "mixed": "혼합 언어", "und": "판별 불가",
        }
        totals = {}
        for language, verdict, count in label_stats:
            totals.setdefault(language, {"normal": 0, "violation": 0})[verdict] = count
        lines = [
            f"{language_names.get(language, language)}: 정상 {values['normal']} · 위반 {values['violation']}"
            for language, values in totals.items()
        ]
        embed.add_field(name="관리자 확정 학습 자료", value="\n".join(lines)[:1024], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="오탐학습")
@commands.has_permissions(administrator=True)
async def show_false_positive_rules(ctx, limit: int = 15):
    """활성 오탐 학습 규칙을 최근 순으로 조회한다."""
    rows = await learning.list_rules(ctx.guild.id, limit=limit)
    if not rows:
        await ctx.send("등록된 오탐 학습 규칙이 없습니다.")
        return
    lines = []
    for rule_id, scope_id, content, level, marked_by, _ in rows:
        scope = "서버 전체" if scope_id == 0 else f"<#{scope_id}>"
        snippet = " ".join((content or "").split())[:80]
        lines.append(
            f"`#{rule_id}` · {scope} · 이전 판단 `{level or 'UNKNOWN'}` · "
            f"등록자 <@{marked_by}>\n> {snippet}"
        )
    embed = discord.Embed(
        title="오탐 학습 규칙",
        description="\n\n".join(lines)[:4000],
        color=discord.Color.green(),
    )
    embed.set_footer(text="취소: !BB 오탐취소 <규칙번호>")
    await ctx.send(embed=embed)


@bot.command(name="오탐취소")
@commands.has_permissions(administrator=True)
async def remove_false_positive_rule(ctx, rule_id: int):
    """잘못 등록한 오탐 학습 규칙을 비활성화한다."""
    if await learning.remove_rule(ctx.guild.id, rule_id):
        await ctx.send(f"✅ 오탐 학습 규칙 `#{rule_id}`을 취소했습니다.")
    else:
        await ctx.send(f"활성 상태인 오탐 학습 규칙 `#{rule_id}`을 찾지 못했습니다.")


def _acquire_single_instance_lock():
    """
    봇 중복 실행 방지. 두 인스턴스가 동시에 돌면 같은 위반에 제재가 두 번 나간다
    (실제 발생했던 사고). 고정 포트를 선점하는 방식이라 프로세스가 죽으면 자동 해제된다.
    """
    try:
        return runtime_lock.acquire_instance_lock()
    except OSError:
        print("⚠️ 봇이 이미 실행 중입니다. 이중 제재를 막기 위해 이 인스턴스는 종료합니다.")
        print("   (기존 봇을 끄려면: 그 창에서 Ctrl+C, 또는 작업 관리자에서 python 종료)")
        sys.exit(3)  # 봇실행.bat이 "중복 실행"을 크래시와 구별해 재시작을 멈추는 신호


if __name__ == "__main__":
    validate_runtime_environment()
    _instance_lock = _acquire_single_instance_lock()
    ollama_ready, ollama_status = ollama_runtime.ensure_ollama_running()
    print(f"[ollama] {ollama_status}")
    if ollama_ready:
        moderator.reset_ollama_breaker()
    bot.run(TOKEN)
