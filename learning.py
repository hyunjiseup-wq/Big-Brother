"""관리자가 확정한 오탐을 범위가 있는 규칙으로 저장하고 재사용한다."""

import asyncio
import hashlib
import re
import time
import unicodedata

import config
import database


_ZERO_WIDTH_PATTERN = re.compile("[\u200b-\u200d\u2060\ufeff]")
_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_MENTION_PATTERN = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>|@everyone|@here", re.IGNORECASE)

# (guild_id, scope_channel_id, content_hash). scope_channel_id=0은 서버 전체 규칙이다.
_hashes: set[tuple[int, int, str]] = set()
_loaded = False
_load_lock = asyncio.Lock()
_examples_cache: dict[tuple[int, int], tuple[float, list[dict] | None]] = {}


def channel_scope_id(channel_or_id) -> int:
    """스레드는 부모 채널, 일반 채널은 자신의 ID를 학습 범위로 사용한다."""
    parent_id = getattr(channel_or_id, "parent_id", None)
    if parent_id:
        return int(parent_id)
    channel_id = getattr(channel_or_id, "id", channel_or_id)
    return int(channel_id)


def _normalize(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    normalized = _ZERO_WIDTH_PATTERN.sub("", normalized)
    return " ".join(normalized.split()).casefold()


def content_hash(content: str) -> str:
    return hashlib.sha256(_normalize(content).encode("utf-8")).hexdigest()


def _safe_prompt_snippet(content: str) -> str:
    """프롬프트 예시는 비신뢰 데이터로 축약하고 멘션/URL/제어문자를 제거한다."""
    text = unicodedata.normalize("NFKC", content)
    text = _ZERO_WIDTH_PATTERN.sub("", text)
    text = _CONTROL_PATTERN.sub(" ", text)
    text = _MENTION_PATTERN.sub("[MENTION]", text)
    text = _URL_PATTERN.sub("[URL]", text)
    return " ".join(text.split())[:config.FALSE_POSITIVE_EXAMPLE_MAX_CHARS]


async def initialize() -> int:
    """기존 오탐 이력을 v2 규칙으로 백필하고 메모리 인덱스를 준비한다."""
    global _loaded
    async with _load_lock:
        migrated = 0
        existing = {
            (int(g), int(scope), h)
            for g, scope, h in await database.get_all_false_positive_rule_keys(
                include_inactive=True
            )
        }
        for guild_id, channel_id, content, level, reason, marked_by in (
                await database.get_false_positive_backfill_rows()):
            if not content or not _normalize(content):
                continue
            scope_id = int(channel_id or 0)
            key = (int(guild_id), scope_id, content_hash(content))
            if key in existing:
                continue
            await database.upsert_false_positive_rule(
                int(guild_id), scope_id, key[2], _safe_prompt_snippet(content),
                level, reason, channel_id, marked_by,
            )
            existing.add(key)
            migrated += 1
        rows = await database.get_all_false_positive_rule_keys()
        _hashes.clear()
        _hashes.update((int(g), int(scope), h) for g, scope, h in rows)
        _examples_cache.clear()
        _loaded = True
        return migrated


async def _ensure_loaded():
    if not _loaded:
        await initialize()


async def is_known_false_positive(guild_id: int, channel_or_id, content: str) -> bool:
    if not content or not _normalize(content):
        return False
    await _ensure_loaded()
    scope_id = channel_scope_id(channel_or_id)
    h = content_hash(content)
    return ((guild_id, 0, h) in _hashes
            or (guild_id, scope_id, h) in _hashes)


async def record_false_positive(guild_id: int, channel_or_id, content: str,
                                wrong_level: str, wrong_reason: str,
                                marked_by: int, *, server_wide: bool = False) -> int | None:
    if not content or not _normalize(content):
        return None
    await _ensure_loaded()
    source_channel_id = int(getattr(channel_or_id, "id", channel_or_id))
    scope_id = 0 if server_wide else channel_scope_id(channel_or_id)
    h = content_hash(content)
    rule_id = await database.upsert_false_positive_rule(
        guild_id, scope_id, h, _safe_prompt_snippet(content), wrong_level, wrong_reason,
        source_channel_id, marked_by,
    )
    _hashes.add((guild_id, scope_id, h))
    _examples_cache.clear()
    return rule_id


async def record_review_false_positive(review_id: int, guild_id: int, channel_or_id,
                                       marked_by: int, action_taken: str,
                                       *, server_wide: bool = False):
    """검수 상태 변경과 학습 규칙 저장을 원자적으로 완료한다."""
    await _ensure_loaded()
    scope_id = 0 if server_wide else channel_scope_id(channel_or_id)

    # 해시는 원문 전체로 계산해야 하므로 검수 레코드에서 먼저 읽는다.
    content = await database.get_violation_content(review_id, guild_id)
    if not content or not _normalize(content):
        return None
    h = content_hash(content)
    result = await database.resolve_review_as_false_positive(
        review_id, guild_id, scope_id, h, _safe_prompt_snippet(content),
        marked_by, action_taken,
    )
    if result:
        _hashes.add((guild_id, scope_id, h))
        _examples_cache.clear()
    return result


async def get_prompt_examples(guild_id: int, channel_or_id) -> list[dict] | None:
    """현재 채널에 적용되는 최근 사례를 구조화된 비신뢰 데이터로 반환한다."""
    if config.FALSE_POSITIVE_PROMPT_EXAMPLES <= 0:
        return None
    await _ensure_loaded()
    scope_id = channel_scope_id(channel_or_id)
    key = (guild_id, scope_id)
    now = time.time()
    cached = _examples_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    rows = await database.get_recent_false_positive_rules(
        guild_id, scope_id, limit=config.FALSE_POSITIVE_PROMPT_EXAMPLES,
    )
    examples = [
        {
            "scope": "server" if row_scope == 0 else "channel",
            "content": _safe_prompt_snippet(content),
            "previous_level": wrong_level or "UNKNOWN",
        }
        for _, row_scope, content, wrong_level, _ in rows
        if _safe_prompt_snippet(content)
    ] or None
    _examples_cache[key] = (now + config.FALSE_POSITIVE_REFRESH_SECONDS, examples)
    return examples


async def list_rules(guild_id: int, limit: int = 20):
    return await database.list_false_positive_rules(guild_id, max(1, min(limit, 50)))


async def remove_rule(guild_id: int, rule_id: int) -> bool:
    removed = await database.deactivate_false_positive_rule(guild_id, rule_id)
    if removed:
        global _loaded
        _loaded = False
        _examples_cache.clear()
        await _ensure_loaded()
    return removed


def _reset_for_tests():
    global _loaded
    _hashes.clear()
    _examples_cache.clear()
    _loaded = False
