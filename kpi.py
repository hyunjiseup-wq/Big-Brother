"""BB봇 검수 카드의 비식별 KPI 집계·기간 정산·대시보드 전송 자료 생성."""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Callable

import database


KST = datetime.timezone(datetime.timedelta(hours=9))

CATEGORY_LABELS = {
    "language_etiquette": "말투·욕설·예절",
    "rmt_barter": "현금거래·물물교환",
    "ads_links_invites": "홍보·링크·초대",
    "conflict_mockery": "갈등·저격·조롱",
    "cheat": "핵·치트",
    "politics": "정치",
    "platform_safety": "플랫폼 안전·유해 콘텐츠",
    "other_complex": "기타·복합",
}

_CATEGORY_PATTERNS = (
    ("rmt_barter", re.compile(
        r"현금|계좌|송금|입금|거래|디엠|\bDM\b|플리마켓|루블|달러|유로|만원|가격|상품권|페이팔",
        re.IGNORECASE,
    )),
    ("ads_links_invites", re.compile(
        r"홍보|광고|링크|초대|프로모션|유튜브|사이트|서버\s*유도|추천인|제휴",
        re.IGNORECASE,
    )),
    ("language_etiquette", re.compile(
        r"욕설|비속어|반말|존댓말|말투|경어|무례|모욕|패드립|금칙어|예절|음슴체|용용체",
        re.IGNORECASE,
    )),
    ("conflict_mockery", re.compile(
        r"시비|조롱|분란|도발|저격|괴롭|비방|갈등|분탕|공격적",
        re.IGNORECASE,
    )),
    ("cheat", re.compile(r"핵|치트|불공정|핵버스", re.IGNORECASE)),
    ("politics", re.compile(r"정치|정당|대통령|국회의원", re.IGNORECASE)),
    ("platform_safety", re.compile(
        r"혐오|차별|성적\s*콘텐츠|폭력|위협|신상|개인정보|디스코드\s*가이드라인",
        re.IGNORECASE,
    )),
)


def categorize_detection(rule_violated: str | None, reason: str | None) -> str:
    """저장된 규정·사유를 공개 가능한 안정적 KPI 범주로 축약한다."""
    text = f"{rule_violated or ''} {reason or ''}"
    for key, pattern in _CATEGORY_PATTERNS:
        if pattern.search(text):
            return key
    return "other_complex"


@dataclass(frozen=True)
class KpiPeriod:
    kind: str
    key: str
    label: str
    start: datetime.datetime
    end: datetime.datetime

    @property
    def start_timestamp(self) -> float:
        return self.start.timestamp()

    @property
    def end_timestamp(self) -> float:
        return self.end.timestamp()


def _month_shift(year: int, month: int, offset: int) -> tuple[int, int]:
    zero_based = year * 12 + (month - 1) + offset
    return zero_based // 12, zero_based % 12 + 1


def completed_period(kind: str, now: datetime.datetime | None = None) -> KpiPeriod:
    """현재 시각 직전에 완전히 종료된 월/분기/연도 기간을 반환한다."""
    current = (now or datetime.datetime.now(KST)).astimezone(KST)
    if kind == "month":
        year, month = _month_shift(current.year, current.month, -1)
        start = datetime.datetime(year, month, 1, tzinfo=KST)
        end_year, end_month = _month_shift(year, month, 1)
        end = datetime.datetime(end_year, end_month, 1, tzinfo=KST)
        return KpiPeriod("month", f"{year:04d}-{month:02d}", f"{year}년 {month}월", start, end)
    if kind == "quarter":
        current_quarter_start = ((current.month - 1) // 3) * 3 + 1
        end = datetime.datetime(current.year, current_quarter_start, 1, tzinfo=KST)
        year, month = _month_shift(end.year, end.month, -3)
        start = datetime.datetime(year, month, 1, tzinfo=KST)
        quarter = (month - 1) // 3 + 1
        return KpiPeriod("quarter", f"{year:04d}-Q{quarter}",
                         f"{year}년 {quarter}분기", start, end)
    if kind == "year":
        year = current.year - 1
        return KpiPeriod(
            "year", str(year), f"{year}년",
            datetime.datetime(year, 1, 1, tzinfo=KST),
            datetime.datetime(year + 1, 1, 1, tzinfo=KST),
        )
    raise ValueError(f"지원하지 않는 KPI 기간: {kind}")


def current_period(kind: str, now: datetime.datetime | None = None) -> KpiPeriod:
    """관리자 수동 조회용 현재 월/분기/연도 기간."""
    current = (now or datetime.datetime.now(KST)).astimezone(KST)
    if kind == "month":
        start = datetime.datetime(current.year, current.month, 1, tzinfo=KST)
        year, month = _month_shift(current.year, current.month, 1)
        return KpiPeriod(
            "month", f"{current.year:04d}-{current.month:02d}",
            f"{current.year}년 {current.month}월(진행 중)", start,
            datetime.datetime(year, month, 1, tzinfo=KST),
        )
    if kind == "quarter":
        month = ((current.month - 1) // 3) * 3 + 1
        start = datetime.datetime(current.year, month, 1, tzinfo=KST)
        year, end_month = _month_shift(current.year, month, 3)
        quarter = (month - 1) // 3 + 1
        return KpiPeriod(
            "quarter", f"{current.year:04d}-Q{quarter}",
            f"{current.year}년 {quarter}분기(진행 중)", start,
            datetime.datetime(year, end_month, 1, tzinfo=KST),
        )
    if kind == "year":
        start = datetime.datetime(current.year, 1, 1, tzinfo=KST)
        return KpiPeriod(
            "year", str(current.year), f"{current.year}년(진행 중)", start,
            datetime.datetime(current.year + 1, 1, 1, tzinfo=KST),
        )
    raise ValueError(f"지원하지 않는 KPI 기간: {kind}")


def _percent(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _rank(counter: Counter, labeler: Callable[[object], str], limit: int = 8) -> list[dict]:
    return [
        {"key": str(key), "label": labeler(key), "count": count}
        for key, count in counter.most_common(limit)
    ]


async def build_summary(guild_id: int, period: KpiPeriod,
                        channel_labels: dict[int, str] | None = None) -> dict:
    """월/분기/연도 대시보드와 Discord 정산에 공통으로 쓰는 KPI를 만든다."""
    rows = await database.get_kpi_rows(
        guild_id, period.start_timestamp, period.end_timestamp
    )
    channel_labels = channel_labels or {}
    status = Counter(row["review_status"] for row in rows)
    effective = [row for row in rows if row["review_status"] != "superseded"]
    confirmed = status["confirmed"]
    false_positive = status["false_positive"]
    resolved = confirmed + false_positive
    pending = status["pending"] + status["processing"]

    category_all = Counter()
    category_fp = Counter()
    category_tp = Counter()
    channel_all = Counter()
    channel_fp = Counter()
    channel_tp = Counter()
    provider = defaultdict(Counter)
    levels = Counter()
    languages = Counter()
    sources = Counter()
    turnaround_hours = []

    for row in effective:
        category = categorize_detection(row["rule_violated"], row["reason"])
        category_all[category] += 1
        channel_key = row["channel_group"] or row["channel_name"] or row["channel_id"]
        channel_all[channel_key] += 1
        levels[row["level"]] += 1
        languages[row["language_group"]] += 1
        sources[row["detection_source"]] += 1
        provider[row["provider"]]["detected"] += 1
        verdict = row["review_status"]
        if verdict == "false_positive":
            category_fp[category] += 1
            channel_fp[channel_key] += 1
            provider[row["provider"]]["false_positive"] += 1
        elif verdict == "confirmed":
            category_tp[category] += 1
            channel_tp[channel_key] += 1
            provider[row["provider"]]["confirmed"] += 1
        if verdict in {"false_positive", "confirmed"} and row["reviewed_at"]:
            turnaround_hours.append(max(0.0, (row["reviewed_at"] - row["created_at"]) / 3600))

    def channel_label(channel_key) -> str:
        if isinstance(channel_key, str):
            return channel_key
        return channel_labels.get(int(channel_key), "삭제·이전 또는 미확인 채널")

    provider_rows = []
    for name, counts in sorted(provider.items(), key=lambda item: -item[1]["detected"]):
        provider_resolved = counts["confirmed"] + counts["false_positive"]
        provider_rows.append({
            "provider": name,
            "detected": counts["detected"],
            "confirmed": counts["confirmed"],
            "false_positive": counts["false_positive"],
            "precision_percent": _percent(counts["confirmed"], provider_resolved),
        })

    operational = await database.get_kpi_operational_counts(guild_id)
    audit = await database.get_audit_metrics(
        guild_id, period.start_timestamp, period.end_timestamp
    )
    return {
        "schema_version": 1,
        "period": {
            "type": period.kind,
            "key": period.key,
            "label": period.label,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
        },
        "cards": {
            "detected": len(effective),
            "delivered": sum(int(row["card_delivered"]) for row in effective),
            "delivery_failed": sum(not int(row["card_delivered"]) for row in effective),
            "confirmed": confirmed,
            "false_positive": false_positive,
            "pending": pending,
            "superseded": status["superseded"],
            "resolved": resolved,
            "precision_percent": _percent(confirmed, resolved),
            "false_positive_percent": _percent(false_positive, resolved),
            "resolution_percent": _percent(resolved, len(effective)),
        },
        "review_time_hours": {
            "median": round(_percentile(turnaround_hours, 0.5), 2)
            if turnaround_hours else None,
            "p90": round(_percentile(turnaround_hours, 0.9), 2)
            if turnaround_hours else None,
        },
        "top": {
            "detected_categories": _rank(
                category_all, lambda key: CATEGORY_LABELS.get(key, str(key))
            ),
            "false_positive_categories": _rank(
                category_fp, lambda key: CATEGORY_LABELS.get(key, str(key))
            ),
            "confirmed_categories": _rank(
                category_tp, lambda key: CATEGORY_LABELS.get(key, str(key))
            ),
            "detected_channels": _rank(channel_all, channel_label),
            "false_positive_channels": _rank(channel_fp, channel_label),
            "confirmed_channels": _rank(channel_tp, channel_label),
        },
        "providers": provider_rows,
        "levels": dict(levels),
        "languages": dict(languages),
        "sources": dict(sources),
        "audit": audit,
        "operations": operational,
    }


async def get_period_summary(guild_id: int, period: KpiPeriod,
                             channel_labels: dict[int, str] | None = None,
                             *, force_refresh: bool = False,
                             now: datetime.datetime | None = None) -> dict:
    """종료 기간은 스냅샷을 재사용하고 현재 기간은 실시간으로 다시 집계한다."""
    current = (now or datetime.datetime.now(KST)).astimezone(KST)
    finalized = period.end <= current
    if finalized and not force_refresh:
        cached = await database.get_kpi_period_snapshot(
            guild_id, period.kind, period.key
        )
        if cached and not cached["dirty"] and cached["summary_json"]:
            return json.loads(cached["summary_json"])

    summary = await build_summary(guild_id, period, channel_labels)
    if finalized:
        await database.upsert_kpi_period_snapshot(
            guild_id, period.kind, period.key,
            period.start_timestamp, period.end_timestamp,
            json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            summary["cards"]["detected"], True,
        )
    return summary


def build_sync_event(row: dict, channel_label: str | None = None,
                     channel_group: str | None = None) -> dict:
    """사이트 D1로 보낼 최소 KPI 이벤트. Discord/사용자/메시지 식별자는 제외한다."""
    status = row["review_status"]
    verdict = (
        "confirmed" if status == "confirmed"
        else "false_positive" if status == "false_positive"
        else "pending" if status in {"pending", "processing"}
        else status
    )
    return {
        # 여러 서버가 한 수집기를 써도 로컬 PK가 충돌하지 않게 익명 서버 범위를 붙인다.
        # Discord 서버 ID 원문은 사이트로 보내지 않는다.
        "event_id": (
            hashlib.sha256(str(row["guild_id"]).encode("ascii")).hexdigest()[:16]
            + f":{row['review_id']}"
        ),
        "detected_at": datetime.datetime.fromtimestamp(
            row["created_at"], KST
        ).isoformat(),
        "reviewed_at": datetime.datetime.fromtimestamp(
            row["reviewed_at"], KST
        ).isoformat() if row["reviewed_at"] else None,
        "verdict": verdict,
        "level": row["level"],
        "category": categorize_detection(row["rule_violated"], row["reason"]),
        "provider": row["provider"],
        "source": row["detection_source"],
        "language": row["language_group"],
        "card_delivered": bool(row["card_delivered"]),
        "channel": channel_label or row.get("channel_name") or "삭제·이전 채널",
        "channel_group": (
            channel_group or row.get("channel_group") or channel_label
            or row.get("channel_name") or "삭제·이전 채널"
        ),
    }


def build_sync_audit_event(row: dict) -> dict:
    """외부 대시보드용 비식별 배치 감사 실행 요약."""
    anonymous_scope = hashlib.sha256(
        str(row["guild_id"]).encode("ascii")
    ).hexdigest()[:16]
    reviewed = int(row["reviewed_messages"])
    flagged = int(row["flagged_messages"])
    targets = int(row["target_channels"])
    failed = int(row["failed_channels"])
    return {
        "event_id": f"{anonymous_scope}:audit:{row['audit_run_id']}",
        "created_at": datetime.datetime.fromtimestamp(row["created_at"], KST).isoformat(),
        "backend": row["backend"],
        "reviewed_messages": reviewed,
        "flagged_messages": flagged,
        "flag_rate_percent": _percent(flagged, reviewed),
        "failed_channels": failed,
        "target_channels": targets,
        "successful_channel_percent": _percent(max(0, targets - failed), targets),
    }


def build_sync_operations(guild_id: int, counts: dict,
                          captured_at: datetime.datetime | None = None) -> dict:
    """봇이 꺼진 뒤에도 마지막으로 확인된 운영 상태를 보여주는 비식별 스냅샷."""
    anonymous_scope = hashlib.sha256(str(guild_id).encode("ascii")).hexdigest()[:16]
    captured = (captured_at or datetime.datetime.now(KST)).astimezone(KST)
    return {
        "scope": anonymous_scope,
        "captured_at": captured.isoformat(),
        "pending_over_24h": int(counts["pending_over_24h"]),
        "pending_over_72h": int(counts["pending_over_72h"]),
        "active_learning_rules": int(counts["active_learning_rules"]),
        "ai_retry_queue": int(counts["ai_retry_queue"]),
        "kpi_sync_pending": int(counts["kpi_sync_pending"]),
    }


def build_sync_sanction(row: dict) -> dict:
    """비밀번호 보호 스태프 원장으로 보낼 제재 생명주기 기록."""
    anonymous_scope = hashlib.sha256(
        str(row["guild_id"]).encode("ascii")
    ).hexdigest()[:16]

    def iso(value):
        return (
            datetime.datetime.fromtimestamp(float(value), KST).isoformat()
            if value is not None else None
        )

    return {
        "event_id": f"{anonymous_scope}:sanction:{row['sanction_id']}",
        "user_id": str(row["user_id"]),
        "user_display": row["user_display"],
        "action_type": row["action_type"],
        "reason": row["reason"],
        "source": row["source"],
        "status": row["status"],
        "issued_at": iso(row["issued_at"]),
        "expires_at": iso(row["expires_at"]),
        "released_at": iso(row["released_at"]),
        "issued_by_id": str(row["issued_by_id"]) if row["issued_by_id"] else None,
        "issued_by_display": row["issued_by_display"],
        "released_by_id": (
            str(row["released_by_id"]) if row["released_by_id"] else None
        ),
        "released_by_display": row["released_by_display"],
        "release_reason": row["release_reason"],
        "origin_key": row.get("dedupe_key"),
    }


def concise_report_lines(summary: dict) -> list[str]:
    """Discord 정산 임베드에 넣을 짧은 한국어 요약."""
    cards = summary["cards"]
    precision = "-" if cards["precision_percent"] is None else f"{cards['precision_percent']:.1f}%"
    fp_rate = (
        "-" if cards["false_positive_percent"] is None
        else f"{cards['false_positive_percent']:.1f}%"
    )
    top_detected = summary["top"]["detected_categories"]
    top_tp_channel = summary["top"]["confirmed_channels"]
    top_fp_channel = summary["top"]["false_positive_channels"]
    return [
        f"탐지 카드 **{cards['detected']}건** · 처리 {cards['resolved']}건 · 미처리 {cards['pending']}건",
        f"정탐 **{cards['confirmed']}건** · 오탐 **{cards['false_positive']}건**",
        f"정밀도 **{precision}** · 오탐률 **{fp_rate}**",
        "최다 탐지 유형: " + (
            f"**{top_detected[0]['label']} {top_detected[0]['count']}건**" if top_detected else "없음"
        ),
        "정탐 최다 채널: " + (
            f"**{top_tp_channel[0]['label']} {top_tp_channel[0]['count']}건**"
            if top_tp_channel else "없음"
        ),
        "오탐 최다 채널: " + (
            f"**{top_fp_channel[0]['label']} {top_fp_channel[0]['count']}건**"
            if top_fp_channel else "없음"
        ),
    ]
