"""
1차 필터: 외부 AI 호출 전에 정규식/키워드로 빠르게 걸러내는 모듈.

목적: 트래픽이 큰 서버에서 모든 메시지를 AI에 보내면 비용/지연시간이 폭증하므로,
      명백한 케이스(금칙어, 스팸, 초대링크, 너무 짧은 메시지)는 여기서 즉시 처리하고
      AI 판단이 정말 필요한 애매한 메시지만 다음 단계로 넘긴다.
"""
import re
import time
import unicodedata
from collections import defaultdict, deque

import config

INVITE_PATTERN = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/\S+", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
ZERO_WIDTH_PATTERN = re.compile("[\u200b-\u200d\u2060\ufeff]")

# 유저별 최근 메시지 기록 (스팸/도배 감지용): {(guild_id, user_id): deque[(timestamp, content)]}
_recent_messages: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=20))

# 메모리 관리: 유저 수가 계속 늘어나는 서버에서 _recent_messages의 키가 무한히 쌓이는 것을 막기 위해
# 일정 횟수 호출마다 "오래된 유저" 항목을 정리한다.
_CLEANUP_EVERY_N_CALLS = 5000       # 이 횟수 호출마다 정리 수행
_STALE_AFTER_SECONDS = 3600         # 마지막 메시지가 1시간 이전인 유저 기록은 삭제
_call_counter = 0


def _maybe_cleanup():
    global _call_counter
    _call_counter += 1
    if _call_counter % _CLEANUP_EVERY_N_CALLS != 0:
        return
    now = time.time()
    stale_keys = [
        key for key, history in _recent_messages.items()
        if not history or (now - history[-1][0]) > _STALE_AFTER_SECONDS
    ]
    for key in stale_keys:
        del _recent_messages[key]
    if stale_keys:
        print(f"[filters] 스팸 감지 기록 정리: 비활성 유저 {len(stale_keys)}명 제거 "
              f"(현재 추적 중: {len(_recent_messages)}명)")


class FilterResult:
    """
    decision:
      - "SKIP"      : 확실히 정상, AI 호출 불필요
      - "DECIDED"   : 필터만으로 위반 등급이 이미 확정됨 (AI 호출 불필요)
      - "NEEDS_AI"  : 애매해서 AI 판단이 필요함
    """
    def __init__(self, decision: str, level: str = "NONE", reason: str = ""):
        self.decision = decision
        self.level = level
        self.reason = reason


def _is_spam(guild_id: int, user_id: int, content: str) -> bool:
    key = (guild_id, user_id)
    now = time.time()
    history = _recent_messages[key]
    history.append((now, content))

    recent_same = [
        c for t, c in history
        if now - t <= config.SPAM_WINDOW_SECONDS and c.strip() == content.strip()
    ]
    return len(recent_same) >= config.SPAM_REPEAT_THRESHOLD


def _normalize_text(content: str) -> str:
    """우회에 자주 쓰이는 호환 문자와 제로폭 문자를 정규화한다."""
    normalized = unicodedata.normalize("NFKC", content)
    return ZERO_WIDTH_PATTERN.sub("", normalized).strip()


def fast_check(guild_id: int, user_id: int, content: str) -> FilterResult:
    _maybe_cleanup()
    text = _normalize_text(content)

    if not text:
        return FilterResult("SKIP")

    lowered = text.lower()

    # 명백한 심각 금칙어
    for word in config.BANNED_WORDS_SEVERE:
        normalized_word = _normalize_text(word).lower()
        if normalized_word and normalized_word in lowered:
            return FilterResult("DECIDED", "SEVERE", f"금칙어 감지: 규칙 위반 단어 포함")

    # 초대 링크 (허용 안 하는 정책이면 즉시 MODERATE 처리)
    if config.BLOCK_DISCORD_INVITES and INVITE_PATTERN.search(text):
        return FilterResult("DECIDED", "MODERATE", "디스코드 초대 링크 무단 게시")

    # 도배/스팸 (동일 메시지 반복)
    if _is_spam(guild_id, user_id, text):
        return FilterResult("DECIDED", "MODERATE", "짧은 시간 내 동일 메시지 반복 (도배)")

    # 경미 금칙어
    for word in config.BANNED_WORDS_MODERATE:
        normalized_word = _normalize_text(word).lower()
        if normalized_word and normalized_word in lowered:
            return FilterResult("DECIDED", "MINOR", "경미 금칙어 감지")

    # 길이는 결정적 필터를 모두 통과한 뒤 AI 호출 여부에만 사용한다.
    # 한국어 욕설은 2~3글자인 경우가 많아 이 검사를 앞에 두면 필터 전체가 우회된다.
    if len(text) < config.MIN_LENGTH_FOR_AI_CHECK:
        return FilterResult("SKIP")

    # 여기까지 왔으면 명확히 판단 불가 -> AI에게 위임
    return FilterResult("NEEDS_AI")
