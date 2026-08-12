"""
배치 감사(Batch Audit) 모듈.

실시간 자동제재(bot.py)와 별개로, 등록된 채널들의 과거 대화를 주기적으로 모아
운영 규정에 따라 분류/태깅한 뒤 "리포트"만 만든다. 자동으로 삭제/타임아웃/킥/밴 등의
조치를 실행하지 않는다 — 최종 판단은 관리자가 리포트를 보고 직접 내린다.

두 가지 실행 방식을 모두 지원한다:
1. 통합 실행: bot.py 안에서 discord.ext.tasks 로 주기적으로 자동 실행 (24/7 서버, 이 파일의
   run_full_audit()을 그대로 호출).
2. 독립 실행: `python batch_audit.py --backend ollama` 로 단독 실행.
   예) 개인 PC에서 주 1회, 컴퓨터를 안 쓰는 새벽 시간에 Windows 작업 스케줄러/cron으로 실행.

리포트는 로컬 Markdown 파일로 저장되고, REPORT_CHANNEL_ID가 설정되어 있으면 디스코드 채널에도 전송된다.
"""
import os
import argparse
import asyncio
import datetime
from pathlib import Path
from collections import defaultdict

import discord
from dotenv import load_dotenv

import config
import database
import learning
from moderator import classify_batch, get_channel_note, is_barter_channel

load_dotenv()

_audit_lock = asyncio.Lock()


async def collect_messages(channel: discord.TextChannel, after_message_id):
    """
    체크포인트 이후의 메시지만 가져온다 (봇 메시지/빈 메시지 제외).

    - 체크포인트가 없는 첫 실행: 채널 전체 이력을 긁으면 대형 서버에서 수십만 건이 되어
      실행이 수 시간 걸리고 API 한도에 걸릴 수 있으므로, 최근
      config.BATCH_FIRST_RUN_LOOKBACK_DAYS일 이내로 범위를 제한한다.
    - 모든 실행: 채널당 config.BATCH_MAX_MESSAGES_PER_CHANNEL건까지만 수집한다.
      상한에 걸리면 체크포인트가 거기까지만 전진하므로, 다음 실행에서 이어서 처리된다.
    """
    if after_message_id:
        after = discord.Object(id=after_message_id)
    else:
        after = discord.utils.utcnow() - datetime.timedelta(days=config.BATCH_FIRST_RUN_LOOKBACK_DAYS)

    messages = []
    truncated = False
    async for msg in channel.history(after=after, limit=None, oldest_first=True):
        if msg.author.bot or not msg.content.strip():
            continue
        messages.append(msg)
        if len(messages) >= config.BATCH_MAX_MESSAGES_PER_CHANNEL:
            truncated = True
            break

    if truncated:
        print(f"[batch_audit] #{channel.name}: 수집 상한({config.BATCH_MAX_MESSAGES_PER_CHANNEL}건) 도달. "
              f"나머지는 다음 실행에서 이어서 처리됩니다.")
    return messages


def _chunk(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


async def collect_barter_conversation_context(channel, before_message) -> list[tuple]:
    """체크포인트·배치 경계를 넘어 물물교환 글의 선행 대화를 제한된 크기로 가져온다."""
    remaining_chars = config.BARTER_CONTEXT_MAX_CHARS
    collected = []
    starter_entry = None
    starter_id = None

    if getattr(channel, "parent", None) is not None:
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is not None and getattr(channel, "id", None) != before_message.id:
            try:
                starter = await fetch_message(channel.id)
                content = (getattr(starter, "content", "") or "").strip()
                if content and not getattr(getattr(starter, "author", None), "bot", False):
                    content = content[:min(2000, remaining_chars)]
                    starter_id = getattr(starter, "id", None)
                    starter_entry = (
                        getattr(getattr(starter, "author", None), "id", None), content
                    )
                    remaining_chars -= len(content)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    try:
        async for prior in channel.history(
                limit=config.BARTER_CONTEXT_MESSAGE_LIMIT,
                before=before_message,
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
                content = "…" if remaining_chars == 1 else "…" + content[-(remaining_chars - 1):]
            collected.append((getattr(getattr(prior, "author", None), "id", None), content))
            remaining_chars -= len(content)
    except (discord.Forbidden, discord.HTTPException):
        return [starter_entry] if starter_entry else []

    collected.reverse()
    if starter_entry:
        collected.insert(0, starter_entry)
    return collected


async def audit_channel(channel: discord.TextChannel, backend: str) -> dict:
    """한 채널을 감사하고 결과(검토 건수 + 플래그된 메시지 목록)를 반환한다."""
    checkpoint = await database.get_checkpoint(channel.guild.id, channel.id)
    messages = await collect_messages(channel, checkpoint)

    if not messages:
        return {"channel": channel, "flagged": [], "reviewed_count": 0,
                "period_start": None, "period_end": None}

    channel_note = get_channel_note(channel)  # 채널별 특수 규칙 (없으면 None)
    barter_mode = is_barter_channel(channel)
    # 과거 오탐 사례를 프롬프트에 포함해 같은 유형의 오탐을 줄인다
    fp_examples = await learning.get_prompt_examples(channel.guild.id, channel)
    flagged = []
    processed_messages = []
    failure = None
    for batch in _chunk(messages, config.BATCH_SIZE):
        # 확정 오탐은 AI에 보내지 않아 재오탐과 API 비용을 함께 줄인다.
        known_false_positives = [
            await learning.is_known_false_positive(channel.guild.id, channel, m.content)
            for m in batch
        ]
        if all(known_false_positives):
            processed_messages.extend(batch)
            continue

        # 물물교환에서는 정상 확정 메시지도 대화 의미를 구성할 수 있으므로 현재 묶음 전체를
        # 문맥에 남기고, 결과를 기록할 때만 확정 오탐 항목을 제외한다.
        if barter_mode:
            candidates = list(batch)
            candidate_known_flags = known_false_positives
        else:
            candidates = [m for m, known in zip(batch, known_false_positives) if not known]
            candidate_known_flags = [False] * len(candidates)

        author_refs = {}
        context_turns = []
        if barter_mode:
            prior_turns = await collect_barter_conversation_context(channel, batch[0])
            for author_id, content in prior_turns:
                author_ref = author_refs.setdefault(
                    author_id, f"user_{len(author_refs) + 1}"
                )
                context_turns.append({"speaker": author_ref, "content": content})
        payload = [
            {
                "index": i,
                "author_ref": author_refs.setdefault(m.author.id, f"user_{len(author_refs) + 1}"),
                "content": m.content,
            }
            for i, m in enumerate(candidates)
        ]
        try:
            results = await classify_batch(payload, backend=backend,
                                           channel_note=channel_note, fp_examples=fp_examples,
                                           barter_context=barter_mode,
                                           conversation_context=context_turns)
        except Exception as e:
            failure = str(e)[:500]
            print(f"[batch_audit] #{channel.name} 배치 판단 실패, 체크포인트 보류: {failure}")
            break

        for r, m, known in zip(results, candidates, candidate_known_flags):
            if known or r.level == "NONE":
                continue
            flagged.append({
                "message": m,
                "level": r.level,
                "rule_violated": r.rule_violated,
                "reason": r.reason,
                "provider": r.provider,
            })
        processed_messages.extend(batch)

    # 성공적으로 판단한 마지막 배치까지만 체크포인트를 전진시킨다.
    # 실패한 배치는 다음 감사에서 반드시 다시 처리된다.
    if processed_messages:
        await database.set_checkpoint(channel.guild.id, channel.id, processed_messages[-1].id)

    return {
        "channel": channel,
        "flagged": flagged,
        "reviewed_count": len(processed_messages),
        "period_start": processed_messages[0].created_at if processed_messages else None,
        "period_end": processed_messages[-1].created_at if processed_messages else None,
        "error": failure,
    }


_KST = datetime.timezone(datetime.timedelta(hours=9))


def _fmt_kst(dt: datetime.datetime) -> str:
    """UTC 시각을 한국 시간 'MM/DD HH:MM' 형태로 표기한다."""
    return dt.astimezone(_KST).strftime("%m/%d %H:%M")


def build_report_markdown(audit_results: list) -> str:
    """감사 결과들을 하나의 Markdown 리포트로 정리한다."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 채팅 감사 리포트 ({now})", ""]

    total_reviewed = sum(r["reviewed_count"] for r in audit_results)
    total_flagged = sum(len(r["flagged"]) for r in audit_results)
    failed_channels = sum(bool(r.get("error")) for r in audit_results)
    lines.append(f"- 검토한 메시지: {total_reviewed}건")
    lines.append(f"- 규정 위반 의심: {total_flagged}건")
    if failed_channels:
        lines.append(f"- ⚠️ 판단 실패로 재시도가 필요한 채널: {failed_channels}개")
    lines.append("")

    # 채널별 검토 범위: 어느 채널의 언제부터 언제까지 대화를 몇 건 검토했는지
    lines.append("### 채널별 검토 범위")
    for r in audit_results:
        name = f"#{r['channel'].name}"
        if r.get("error"):
            lines.append(
                f"- {name}: ⚠️ 일부 또는 전체 판단 실패 · 성공 처리 {r['reviewed_count']}건 · "
                "실패 배치는 체크포인트를 전진시키지 않음"
            )
        elif r["reviewed_count"] == 0:
            lines.append(f"- {name}: 지난 감사 이후 새 메시지 없음")
        else:
            lines.append(
                f"- {name}: {_fmt_kst(r['period_start'])} ~ {_fmt_kst(r['period_end'])} (한국 시간) · "
                f"{r['reviewed_count']}건 검토 · 위반 의심 {len(r['flagged'])}건"
            )
    lines.append("")

    by_user = defaultdict(list)
    for r in audit_results:
        for f in r["flagged"]:
            by_user[f["message"].author].append((r["channel"], f))

    if not by_user:
        lines.append("이번 주기에는 규정 위반으로 의심되는 메시지가 없습니다. ✅")
        return "\n".join(lines)

    lines.append("## 유저별 정리\n")
    for user, items in sorted(by_user.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"### {user} (`{user.id}`) — {len(items)}건\n")
        for channel, f in items:
            msg = f["message"]
            snippet = msg.content[:200].replace("\n", " ")
            lines.append(
                f"- **[{f['level']}]** #{channel.name} · 규정 {f['rule_violated']} · "
                f"판단모델: {f['provider']}\n"
                f"  - 사유: {f['reason']}\n"
                f"  - 원문: > {snippet}\n"
                f"  - 링크: {msg.jump_url}\n"
            )
    return "\n".join(lines)


def save_report_file(report_text: str) -> str:
    os.makedirs(config.REPORT_OUTPUT_DIR, exist_ok=True)
    filename = f"audit_report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    path = os.path.join(config.REPORT_OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_text)
    return path


def prune_expired_reports(retention_days: int) -> int:
    """설정된 기간보다 오래된 감사 리포트만 삭제하고 삭제 건수를 반환한다."""
    if retention_days <= 0:
        return 0
    report_dir = Path(config.REPORT_OUTPUT_DIR)
    if not report_dir.is_dir():
        return 0
    cutoff = datetime.datetime.now().timestamp() - retention_days * 86400
    removed = 0
    for path in report_dir.glob("audit_report_*.md"):
        # 링크를 따라 외부 파일을 지우지 않고 일반 파일만 정리한다.
        if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
            continue
        path.unlink()
        removed += 1
    return removed


async def send_report_to_discord(guild: discord.Guild, report_text: str, file_path: str):
    raw = os.environ.get("REPORT_CHANNEL_ID", "").strip()
    channel_id = None
    if raw:
        try:
            channel_id = int(raw)
        except ValueError:
            print(f"[batch_audit] REPORT_CHANNEL_ID 값이 숫자가 아닙니다: {raw!r}")
    if not channel_id:
        print("[batch_audit] REPORT_CHANNEL_ID가 설정되어 있지 않아 디스코드 전송은 건너뜁니다 (로컬 파일만 저장됨).")
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        print(f"[batch_audit] 채널 ID {channel_id}를 찾을 수 없습니다.")
        return
    if not hasattr(channel, "send"):
        print(f"[batch_audit] 리포트 채널 #{channel.name}은(는) {type(channel).__name__}이라 메시지를 보낼 수 없습니다. "
              f"REPORT_CHANNEL_ID를 일반 텍스트 채널 ID로 바꿔주세요 (리포트는 로컬 파일로는 저장됨).")
        return

    summary = report_text.split("## 유저별 정리")[0].strip()
    embed = discord.Embed(title="📋 채팅 감사 리포트", description=summary[:4000], color=discord.Color.blue())
    try:
        await channel.send(embed=embed, file=discord.File(file_path))
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[batch_audit] 채널 {channel_id} 리포트 전송 실패: {type(e).__name__}")


_LEVEL_SEVERITY = {"MINOR": 1, "MODERATE": 2, "SEVERE": 3, "EXTREME": 4}


async def _post_review_cards(audit_results: list, on_flagged):
    """
    위반 의심 메시지들을 제재 로그 채널에 검토 카드로 올린다 (콜백은 bot.py가 제공).
    심각한 등급부터 올리고, config.BATCH_REVIEW_CARD_LIMIT개까지만 카드로 올려 도배를 막는다.

    카드 상한을 넘은 건도 검수 레코드는 DB에 남긴다(card_delivered=0). 감사 체크포인트는
    이미 그 메시지들을 지나가므로, 레코드를 안 남기면 리포트 파일에만 존재하고 다음 감사에서
    다시 카드로 나타나지 않아 영영 검토에서 누락되기 때문이다.
    `!BB 검토대기`에서 '카드 없는 대기 건'으로 확인할 수 있다.
    """
    flagged = [f for r in audit_results for f in r["flagged"]]
    if not flagged:
        return
    flagged.sort(key=lambda f: -_LEVEL_SEVERITY.get(f["level"], 0))

    limit = getattr(config, "BATCH_REVIEW_CARD_LIMIT", 25)
    posted = 0
    stored_only = 0
    for index, f in enumerate(flagged):
        message = f["message"]
        will_post = index < limit
        try:
            review_id = await database.create_review_record(
                message.guild.id, message.author.id, message.channel.id, message.id,
                message.content, f["level"], f["reason"],
                "배치 감사 검토 대기" if will_post else "배치 감사 검토 대기 (카드 상한 초과로 미게시)",
                f["provider"], card_delivered=will_post,
            )
        except Exception as e:
            print(f"[batch_audit] 검수 레코드 저장 실패: {e}")
            continue

        if not will_post:
            stored_only += 1
            continue
        try:
            await on_flagged(message, f["level"], f["reason"], f["rule_violated"],
                             f["provider"], review_id)
            posted += 1
        except Exception as e:
            print(f"[batch_audit] 검토 카드 전송 실패: {e}")
            try:
                await database.mark_review_delivery_failed(review_id, message.guild.id)
            except Exception:
                pass

    print(f"[batch_audit] 검토 카드 {posted}건을 제재 로그 채널에 올렸습니다.")
    if stored_only:
        print(f"[batch_audit] 카드 상한({limit}건)을 넘은 {stored_only}건은 카드 없이 검수 대기로 저장했습니다. "
              f"`!BB 검토대기`에서 확인하세요 (BATCH_REVIEW_CARD_LIMIT 조정 가능).")


async def _expand_audit_targets(guild: discord.Guild) -> list:
    """등록 채널을 실제 메시지 이력을 가진 채널/포럼 게시글 목록으로 확장한다."""
    targets = []
    seen_ids = set()

    def add_target(channel) -> None:
        channel_id = getattr(channel, "id", None)
        if channel_id is None or channel_id in seen_ids or not hasattr(channel, "history"):
            return
        seen_ids.add(channel_id)
        targets.append(channel)

    for channel_id in config.WATCHED_CHANNEL_IDS:
        channel = guild.get_channel(channel_id)
        if channel is None:
            print(f"[batch_audit] 채널 ID {channel_id}를 찾을 수 없습니다 (봇 권한/오타 확인).")
            continue

        if hasattr(channel, "history"):
            add_target(channel)
            continue

        # ForumChannel 자체에는 history가 없고 실제 메시지는 각 Thread에 있다.
        # 활성 게시글과 최근 보관 게시글을 모두 펼쳐야 물물교환 대화 전체가 감사된다.
        active_threads = list(getattr(channel, "threads", ()) or ())
        for thread in active_threads:
            add_target(thread)

        archived_count = 0
        archived_threads = getattr(channel, "archived_threads", None)
        if callable(archived_threads):
            cutoff = discord.utils.utcnow() - datetime.timedelta(
                days=config.BATCH_FIRST_RUN_LOOKBACK_DAYS
            )
            try:
                async for thread in archived_threads(limit=None):
                    archived_at = getattr(thread, "archive_timestamp", None)
                    if archived_at is not None and archived_at < cutoff:
                        break
                    before = len(targets)
                    add_target(thread)
                    if len(targets) > before:
                        archived_count += 1
            except (discord.Forbidden, discord.HTTPException) as e:
                print(
                    f"[batch_audit] #{channel.name} 보관 게시글 조회 실패: "
                    f"{type(e).__name__}"
                )

        if not active_threads and not archived_count:
            print(f"[batch_audit] #{channel.name}: 감사할 활성/최근 보관 게시글이 없습니다.")
        else:
            print(
                f"[batch_audit] #{channel.name}: 활성 게시글 {len(active_threads)}개, "
                f"최근 보관 게시글 {archived_count}개를 감사합니다."
            )

    return targets


async def run_full_audit(guild: discord.Guild, backend: str = None, on_flagged=None) -> str:
    """
    등록된 모든 감시 채널을 감사하고 리포트를 저장 + (설정 시) 디스코드로 전송한다.
    on_flagged가 주어지면, 위반 의심 메시지마다 그 콜백을 호출해 제재 로그에 검토 카드를 올린다
    (bot.py가 post_batch_review_card를 넘겨준다. 독립 실행 모드에서는 None이라 카드를 올리지 않음).
    """
    backend = backend or config.BATCH_BACKEND
    if backend not in {"auto", "gemini", "groq", "ollama"}:
        raise ValueError(f"지원하지 않는 배치 backend: {backend}")

    if _audit_lock.locked():
        raise RuntimeError("이미 다른 배치 감사가 실행 중입니다.")
    await _audit_lock.acquire()

    try:
        if not config.WATCHED_CHANNEL_IDS:
            print("[batch_audit] config.WATCHED_CHANNEL_IDS가 비어 있습니다. 감시할 채널 ID를 등록하세요.")
            return None

        audit_results = []
        for channel in await _expand_audit_targets(guild):
            result = await audit_channel(channel, backend)
            audit_results.append(result)
            print(f"[batch_audit] #{channel.name}: {result['reviewed_count']}건 검토, "
                  f"{len(result['flagged'])}건 플래그 (backend={backend})")

        if not audit_results:
            return None

        report_text = build_report_markdown(audit_results)
        file_path = save_report_file(report_text)
        print(f"[batch_audit] 리포트 저장 완료: {file_path}")

        await send_report_to_discord(guild, report_text, file_path)

        if on_flagged is not None:
            await _post_review_cards(audit_results, on_flagged)

        return file_path
    finally:
        _audit_lock.release()


# ══════════════════════════════════════════════════════════════════
# 독립 실행 모드 — 예) 주 1회 개인 PC에서 로컬 Ollama와 함께 실행
#   python batch_audit.py --backend ollama
# 이 모드는 봇을 상시 로그인 상태로 두지 않고, 한 번 접속해서 감사를 끝낸 뒤 바로 종료한다.
# Windows 작업 스케줄러 / cron / launchd 등으로 주기 실행하도록 등록하면 된다.
# ══════════════════════════════════════════════════════════════════
async def _standalone_main(backend: str):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"[batch_audit] 로그인 완료: {client.user} (backend={backend})")
        for guild in client.guilds:
            await run_full_audit(guild, backend=backend)
        await client.close()

    token = os.environ["DISCORD_BOT_TOKEN"]
    await client.start(token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="디스코드 채팅 배치 감사 (독립 실행)")
    parser.add_argument(
        "--backend", choices=["auto", "gemini", "groq", "ollama"], default=config.BATCH_BACKEND,
        help="판단에 사용할 모델 백엔드 (기본값: config.BATCH_BACKEND). "
             "로컬 PC에서 GPU로 돌릴 때는 --backend ollama 사용.",
    )
    args = parser.parse_args()
    asyncio.run(_standalone_main(args.backend))
