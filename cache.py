"""
동일하거나 거의 같은 메시지 내용에 대한 AI 판단 결과를 잠깐 캐싱한다.
같은 문구가 여러 유저에 의해(레이드/매크로 스팸) 또는 한 유저에 의해 반복될 때
매번 외부 AI API를 호출하지 않도록 하여 비용과 지연시간을 줄인다.
"""
import time
import hashlib
from collections import OrderedDict

import config

# key -> (expire_at, level, rule_violated, reason, provider)
_cache: "OrderedDict[str, tuple[float, str, str, str, str]]" = OrderedDict()


def _make_key(content: str, context: str = "") -> str:
    # context: 채널별 특수 규칙(config.CHANNEL_CONTEXT_NOTES) 등 판단 기준이 달라지는 요소.
    # 같은 문구라도 적용 규칙이 다른 채널이면 캐시를 공유하지 않도록 키에 섞는다.
    normalized = content.strip().lower()
    if context:
        normalized = f"{context}\x00{normalized}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def get(content: str, context: str = ""):
    key = _make_key(content, context)
    entry = _cache.get(key)
    if entry is None:
        return None
    expire_at, level, rule_violated, reason, provider = entry
    if time.time() > expire_at:
        _cache.pop(key, None)
        return None
    # LRU: 최근 사용한 항목을 뒤로 이동
    _cache.move_to_end(key)
    return level, rule_violated, reason, provider


def set(content: str, level: str, rule_violated: str, reason: str, provider: str,
        context: str = ""):
    key = _make_key(content, context)
    _cache[key] = (
        time.time() + config.CACHE_TTL_SECONDS,
        level,
        rule_violated,
        reason,
        provider,
    )
    _cache.move_to_end(key)

    # 캐시 크기 제한 (오래된 항목부터 제거)
    while len(_cache) > config.CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)
