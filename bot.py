"""
디스코드 자동 제재 봇 메인 파일 (대규모 서버 + Gemini/Groq 이중화 버전).

흐름:
1. 메시지 수신 -> filters.fast_check()로 1차 필터링 (정규식/금칙어/스팸, AI 호출 없음)
   - SKIP    : 정상 메시지, 즉시 종료
   - DECIDED : 필터만으로 등급 확정, AI 호출 없이 바로 제재 처리
   - NEEDS_AI: 애매한 경우만 큐에 넣어 워커가 비동기로 AI 판단
2. AI 판단 전, cache에서 동일/반복 문구의 기존 판단 결과가 있는지 먼저 확인
3. moderator.classify_message()가 Gemini를 우선 시도하고, 실패/한도초과 시 Groq로 자동 폴백
4. 위반 등급에 따라 점수 부여 (config.VIOLATION_LEVEL_POINTS)
5. 누적 점수 -> config.STRIKE_THRESHOLDS 에 따라 조치 결정
   단, 커뮤니티 정책상 킥/밴은 판단 주체(필터/Gemini/Groq) 무관하게 자동 실행하지 않고
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
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import datetime

import config
import database
import cache
import learning
from filters import fast_check
import moderator
from moderator import classify_message, get_channel_note
from batch_audit import prune_expired_reports, run_full_audit

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

    for name in ("PUBLIC_LOG_CHANNEL_ID", "REPORT_CHANNEL_ID"):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                if int(raw) <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"{name}는 비워두거나 양의 정수 Discord 채널 ID를 사용해야 합니다.")

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
_processing_keys: set[tuple[int, int, str]] = set()
_background_tasks: set[asyncio.Task] = set()


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
            await member.timeout(until, reason=reason)
            primary_ok = True
            details.append("타임아웃 성공")
        except (discord.Forbidden, discord.HTTPException) as e:
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
            await member.ban(reason=reason, delete_message_days=1)
            primary_ok = True
            details.append("밴 성공")
        except (discord.Forbidden, discord.HTTPException) as e:
            details.append(f"밴 실패: {type(e).__name__}")
    elif action == "DELETE":
        primary_ok = delete_ok

    # 유저에게 DM으로 안내. WARN은 DM 자체가 핵심 조치라 실패 여부를 반영한다.
    if action != "NONE":
        try:
            action_text = {
                "WARN": "경고",
                "DELETE": "메시지 삭제 및 경고",
                "TIMEOUT": f"{duration_minutes}분 타임아웃",
                "KICK": "서버에서 추방",
                "BAN": "서버에서 영구 차단",
            }.get(action, action)
            await member.send(
                f"'{guild.name}' 서버에서 규칙 위반으로 다음 조치가 적용되었습니다: **{action_text}**\n사유: {reason}"
            )
            details.append("DM 성공")
            if action == "WARN":
                primary_ok = True
        except (discord.Forbidden, discord.HTTPException) as e:
            details.append(f"DM 실패: {type(e).__name__}")

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


_PROVIDER_LABEL = {"gemini": "Gemini(1차)", "groq": "Groq(폴백)", "filter": "키워드 필터", "none": "판단 실패"}


async def _handle_violation_review_only(message: discord.Message, level: str, reason_text: str,
                                        rule_violated: str, provider: str, points_to_add: float):
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
        message.content, level, reason_text,
        f"검수모드(조치 없음, 모의: {action_label})", provider,
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
    embed.add_field(name="자동 모드였다면", value=f"{action_label} (점수 {would_be_points:.1f})", inline=False)
    embed.add_field(name="사유", value=reason_text or "-", inline=False)
    embed.add_field(name="원문", value=(message.content[:500] or "(내용 없음)"), inline=False)
    embed.add_field(name="메시지 바로가기", value=message.jump_url, inline=False)
    view = _build_review_view(message.channel.id, message.id, message.author.id, review_id)
    delivered = await send_log(message.guild, embed, view=view)
    if not delivered:
        # 카드가 안 올라가면 관리자는 검수할 방법이 없다. 카드 없음 상태로 표시해
        # `!BB 검토대기`에서 별도로 확인하고 로그 채널 권한을 점검하게 한다.
        await database.mark_review_delivery_failed(review_id, message.guild.id)
        print("⚠️ 검수 카드 전송 실패 — 로그 채널 권한/설정을 확인하세요. "
              "감지 기록은 DB에 남아 `!BB 검토대기`에서 조회됩니다.")


async def post_batch_review_card(message: discord.Message, level: str, reason_text: str,
                                 rule_violated: str, provider: str, review_id: int):
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
    embed.add_field(name="작성 시각", value=discord.utils.format_dt(message.created_at, "f"), inline=True)
    embed.add_field(name="자동 모드였다면", value=f"{action_label} (점수 {would_be_points:.1f})", inline=False)
    embed.add_field(name="사유", value=reason_text or "-", inline=False)
    embed.add_field(name="원문", value=(message.content[:500] or "(내용 없음)"), inline=False)
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
    "warn": "경고 DM",
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
    add("⚠️ 경고 DM", "warn", discord.ButtonStyle.secondary, 0)
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


async def _dm_member(member: discord.Member, guild_name: str, action_text: str, reason_text: str):
    try:
        await member.send(
            f"'{guild_name}' 서버에서 규칙 위반으로 다음 조치가 적용되었습니다: **{action_text}**\n사유: {reason_text}"
        )
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


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
    if action in ("warn", "to1", "to24", "kick") and member is None:
        return False, "대상 유저가 서버에 없어 이 조치를 실행할 수 없습니다."

    if review_id and not await database.claim_review(review_id, guild.id):
        return False, "이미 다른 관리자가 처리했거나 처리 중인 검수 건입니다."

    async def fail(text: str):
        if review_id:
            await database.release_review(review_id, guild.id)
        return False, text

    dm_text = {
        "del": "메시지 삭제 및 경고", "warn": "경고",
        "to1": "60분 타임아웃", "to24": "24시간 타임아웃",
        "kick": "서버에서 추방", "ban": "서버에서 영구 차단",
    }.get(action)
    # 원문 메시지 삭제 (정상/경고 제외 모든 조치에 포함)
    note = ""
    if action in ("del", "to1", "to24", "kick", "ban"):
        channel = guild.get_channel(channel_id)
        try:
            msg = await channel.fetch_message(message_id) if channel and hasattr(channel, "fetch_message") else None
            if msg is not None:
                await msg.delete()
        except discord.NotFound:
            if action == "del":
                note = " (메시지가 이미 삭제되어 있었음)"
        except (discord.Forbidden, discord.HTTPException):
            note = " / 메시지 삭제 실패 (봇 권한 확인)"
            if action == "del":
                return await fail("봇 권한이 부족해 메시지를 삭제하지 못했습니다.")

    try:
        if action in ("to1", "to24"):
            minutes = 60 if action == "to1" else 1440
            until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
            await member.timeout(until, reason=reason)
        elif action == "kick":
            await member.kick(reason=reason)
        elif action == "ban":
            await guild.ban(member or discord.Object(id=user_id), reason=reason)
    except (discord.Forbidden, discord.HTTPException):
        return await fail(
            "봇 권한 또는 Discord API 오류로 조치를 실행하지 못했습니다 "
            "(봇 역할이 대상 유저의 역할보다 위에 있는지 확인)."
        )

    dm_ok = True
    if dm_text and member is not None:
        dm_ok = await _dm_member(member, guild.name, dm_text, reason_text)
        if action == "warn" and not dm_ok:
            return await fail("대상 유저에게 경고 DM을 보낼 수 없어 경고를 적용하지 못했습니다.")

    applied = _REVIEW_ACTION_LABEL[action] + note

    if action not in ("ok", "okg"):
        # 외부 조치가 성공한 즉시 검수를 확정해, 이후 점수/로그 오류가 나더라도
        # 같은 카드 재시도로 제재가 중복 실행되지 않게 한다.
        if review_id:
            await database.resolve_review(
                review_id, guild.id, "confirmed", admin.id, _REVIEW_ACTION_LABEL[action]
            )
        points = config.VIOLATION_LEVEL_POINTS.get(level, 0)
        total = await database.add_points(guild.id, user_id, points)
        applied += f" · 점수 +{points} (누적 {total:.1f})"
        if not review_id:
            await database.log_violation(
                guild.id, user_id, channel_id, original_content, level,
                f"관리자 검수 확정: {reason_text}", _REVIEW_ACTION_LABEL[action],
                provider="admin", needs_review=False, message_id=message_id,
            )
        public_action = {"del": "DELETE", "warn": "WARN", "to1": "TIMEOUT",
                         "to24": "TIMEOUT", "kick": "KICK", "ban": "BAN"}[action]
        await send_public_sanction_log(guild, level, rule, public_action)
    else:  # 관리자가 오탐(정상)으로 확정
        server_wide = action == "okg"
        get_channel = getattr(guild, "get_channel_or_thread", guild.get_channel)
        channel_for_scope = get_channel(channel_id) or channel_id
        content_for_learning = "" if original_content == "(내용 없음)" else original_content
        try:
            if review_id:
                learned = await learning.record_review_false_positive(
                    review_id, guild.id, channel_for_scope, admin.id,
                    _REVIEW_ACTION_LABEL[action], server_wide=server_wide,
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
        try:
            await self.log_message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass
        await interaction.edit_original_response(content=f"✅ 완료: {text}", view=None)
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
    await interaction.message.edit(embed=embed, view=None)


async def handle_violation(message: discord.Message, level: str, reason_text: str,
                            rule_violated: str = "-", provider: str = "filter"):
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
    if latest.content != message.content:
        return
    message = latest

    # 최신 원문이 현재 채널에 적용되는 오탐 규칙과 일치하면 처리하지 않는다.
    if await learning.is_known_false_positive(
            message.guild.id, message.channel, message.content):
        return

    points_to_add = config.VIOLATION_LEVEL_POINTS.get(level, 0)

    # 수동 검수 모드: 감지 결과만 관리자에게 보고하고 여기서 끝낸다 (config.MANUAL_REVIEW_MODE 참고)
    if config.MANUAL_REVIEW_MODE:
        await _handle_violation_review_only(message, level, reason_text, rule_violated, provider, points_to_add)
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


# AI 전량 장애 감지: Gemini/Groq가 둘 다 실패하면 안전하게 NONE 처리되지만(무고한 제재 방지)
# 콘솔에만 찍혀서 관리자가 무감시 상태를 모를 수 있다. 연속 실패가 임계치에 닿으면
# 로그 채널에 경고를 올린다.
# 상태는 서버(guild)별로 따로 센다 — 전역 카운터로 두면 여러 서버에 들어가 있을 때
# 한 서버의 성공이 다른 서버의 연속 실패를 초기화하고, 알림도 엉뚱한 서버로 갈 수 있다.
# {guild_id: {"streak": int, "last_alert": datetime | None}}
_ai_outage_state: dict[int, dict] = {}


async def _track_ai_outage(guild: discord.Guild, result):
    state = _ai_outage_state.setdefault(guild.id, {"streak": 0, "last_alert": None})

    if result.provider != "none":
        if state["streak"] >= config.AI_OUTAGE_ALERT_THRESHOLD:
            print(f"✅ AI 판단이 복구되었습니다 (길드 {guild.id}).")
        state["streak"] = 0
        return

    state["streak"] += 1
    if state["streak"] < config.AI_OUTAGE_ALERT_THRESHOLD:
        return

    now = discord.utils.utcnow()
    cooldown = datetime.timedelta(minutes=config.AI_OUTAGE_ALERT_COOLDOWN_MINUTES)
    if state["last_alert"] and now - state["last_alert"] < cooldown:
        return
    state["last_alert"] = now

    embed = discord.Embed(
        title="🔴 AI 판단 장애 감지",
        description=(
            f"Gemini와 Groq 판단이 **{state['streak']}회 연속 실패**했습니다.\n"
            "실패한 메시지는 안전하게 '위반 없음' 처리되므로 지금 서버는 **키워드 필터만으로 감시 중**입니다.\n\n"
            "확인할 것:\n"
            "1. API 무료 한도 초과 여부 (Gemini는 한국시간 오후 4시경 리셋)\n"
            "2. .env의 GEMINI_API_KEY / GROQ_API_KEY 유효 여부\n"
            "3. 봇 콘솔 창의 오류 메시지"
        ),
        color=discord.Color.red(),
        timestamp=now,
    )
    await send_log(guild, embed, mention=config.ADMIN_REVIEW_MENTION or None)


async def ai_worker(worker_id: int):
    """큐에서 메시지를 꺼내 캐시 확인 후 필요하면 AI(Gemini/Groq)로 판단하는 워커."""
    while True:
        message, enqueued_at, processing_key = await _message_queue.get()
        try:
            queue_age = time.monotonic() - enqueued_at
            if queue_age > config.MAX_QUEUE_AGE_SECONDS:
                print(f"[worker-{worker_id}] {queue_age:.1f}초 지난 메시지를 안전하게 폐기했습니다.")
                _note_drop(message.guild, expired=True)
                continue
            # 관리자가 오탐으로 확정했던 내용과 동일하면 AI 호출 없이 즉시 통과
            # (재오탐 방지 + 무료 API 한도 절약)
            if await learning.is_known_false_positive(
                    message.guild.id, message.channel, message.content):
                # 큐 대기 중 수정된 메시지가 과거 원문 기준으로 통과하지 않게 재확인한다.
                try:
                    latest = await message.channel.fetch_message(message.id)
                except discord.NotFound:
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    latest = None
                if latest is not None and latest.content == message.content:
                    continue
                if latest is not None:
                    message = latest

            # 채널별 특수 규칙 (예: 물물교환 채널은 거래 글이 정상). 캐시 키에도 섞어서
            # 같은 문구가 규칙이 다른 채널의 판단 결과를 재사용하지 않게 한다.
            channel_note = get_channel_note(message.channel)
            cached = cache.get(message.content, context=channel_note or "")
            if cached is not None:
                level, rule_violated, reason_text, provider = cached
                await handle_violation(
                    message, level, f"(캐시된 판단) {reason_text}",
                    rule_violated=rule_violated, provider=provider,
                )
                continue

            # 과거 오탐 사례를 프롬프트에 포함해 같은 유형의 오탐을 줄인다
            fp_examples = await learning.get_prompt_examples(message.guild.id, message.channel)
            async with _ai_semaphore:
                result = await classify_message(message.content, channel_note=channel_note,
                                                fp_examples=fp_examples)

            # 판단 실패(provider="none")는 캐시하지 않는다 — 캐시하면 AI가 복구된 뒤에도
            # 같은 내용의 메시지가 캐시 유효시간 동안 계속 무검사 통과하게 됨
            if result.provider != "none":
                cache.set(
                    message.content, result.level, result.rule_violated,
                    result.reason, result.provider, context=channel_note or "",
                )
            await _track_ai_outage(message.guild, result)
            await handle_violation(message, result.level, result.reason, result.rule_violated, provider=result.provider)
        except Exception as e:
            print(f"[worker-{worker_id}] 처리 중 오류: {e}")
        finally:
            _processing_keys.discard(processing_key)
            _message_queue.task_done()


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
    """봇 종료 시 공유 HTTP 클라이언트를 정리한 뒤 기본 종료 절차를 진행한다."""
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
    migrated = await learning.initialize()
    if migrated:
        print(f"[learning] 기존 오탐 이력 {migrated}건을 범위 지정 규칙으로 이전했습니다.")

    # on_ready는 네트워크 재연결 시마다 다시 불리므로, 워커는 최초 1회만 생성한다.
    # (중복 생성 시 같은 메시지를 여러 워커가 처리해 이중 제재가 발생할 수 있음)
    if not _workers_started:
        for i in range(config.MAX_CONCURRENT_AI_CALLS):
            _spawn(ai_worker(i))
        _workers_started = True

    if config.WATCHED_CHANNEL_IDS and not batch_audit_task.is_running():
        batch_audit_task.start()
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
    return message.guild.id, message.id, message.content


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


def _run_moderation(message: discord.Message):
    """1차 필터 → (필요 시) AI 큐 투입. 새 메시지와 수정된 메시지가 같은 경로를 탄다."""
    processing_key = _message_processing_key(message)
    if processing_key in _processing_keys:
        return

    result = fast_check(message.guild.id, message.author.id, message.content)

    if result.decision == "SKIP":
        pass  # 정상 메시지, 아무 조치 없음
    elif result.decision == "DECIDED":
        # 이벤트 핸들러를 막지 않도록 별도 태스크로 처리 (도배 레이드 시 지연 방지)
        _processing_keys.add(processing_key)
        _spawn(_handle_decided(message, result, processing_key))
    else:  # NEEDS_AI -> 큐에 넣어 워커가 비동기 처리 (여기서 기다리지 않음)
        _processing_keys.add(processing_key)
        try:
            _message_queue.put_nowait((message, time.monotonic(), processing_key))
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

    _run_moderation(message)
    # 명령어는 위에서 조기 처리되므로 여기서는 process_commands를 다시 호출하지 않는다


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    """
    수정된 메시지도 새 메시지와 동일하게 검사한다.
    (깨끗한 메시지를 올린 뒤 욕설로 수정하는 우회 방지)
    raw 이벤트를 쓰는 이유: 기본 on_message_edit은 봇 캐시에 있는 최근 메시지만 잡는데,
    raw는 오래된 메시지를 수정해도 잡힌다.
    """
    # 링크 미리보기(임베드) 생성/고정 등 내용이 안 바뀐 수정 이벤트에는 content 키가 없음
    if "content" not in payload.data:
        return
    new_content = payload.data.get("content") or ""
    if not new_content.strip():
        return
    # 캐시에 수정 전 메시지가 있고 내용이 그대로면 검사 불필요
    if payload.cached_message is not None and payload.cached_message.content == new_content:
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
    _run_moderation(message)


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
            "`!BB 검토대기 [시간]` — 킥/밴 대신 하향 조정된 건 목록 (기본 72시간)\n"
            "`!BB 오탐학습 [개수]` — 활성 오탐 학습 규칙과 적용 범위 조회\n"
            "`!BB 상태` — 처리 대기열/워커/드롭 건수 확인"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ 실행",
        value=(
            "`!BB 점수초기화 @유저` — 유저의 누적 점수 초기화\n"
            "`!BB 오탐취소 <규칙번호>` — 잘못 등록한 오탐 학습 규칙 취소\n"
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

    if not rows and not uncarded:
        await ctx.send(f"✅ 최근 {hours}시간 내 관리자 검토가 필요한 건이 없습니다.")
        return

    provider_label = {"gemini": "Gemini", "groq": "Groq", "filter": "키워드 필터", "none": "판단 실패"}
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
    await ctx.send(embed=embed)


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
    queued = _message_queue.qsize()
    usage = queued / config.MAX_QUEUE_SIZE * 100
    # 큐 사용률이 높다는 건 곧 드롭(감시 구멍)이 시작된다는 신호다.
    gauge = "🟢 여유" if usage < 80 else ("🟡 혼잡" if usage < 95 else "🔴 포화 임박")
    embed.add_field(name="대기열", value=f"{gauge} {queued} / {config.MAX_QUEUE_SIZE} ({usage:.0f}%)", inline=True)
    embed.add_field(name="AI 워커 수", value=str(config.MAX_CONCURRENT_AI_CALLS), inline=True)

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
    import socket
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 57391))
    except OSError:
        print("⚠️ 봇이 이미 실행 중입니다. 이중 제재를 막기 위해 이 인스턴스는 종료합니다.")
        print("   (기존 봇을 끄려면: 그 창에서 Ctrl+C, 또는 작업 관리자에서 python 종료)")
        sys.exit(3)  # 봇실행.bat이 "중복 실행"을 크래시와 구별해 재시작을 멈추는 신호
    return lock


if __name__ == "__main__":
    validate_runtime_environment()
    _instance_lock = _acquire_single_instance_lock()
    bot.run(TOKEN)
