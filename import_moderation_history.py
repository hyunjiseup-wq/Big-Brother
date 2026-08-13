"""기존 moderation-data.json 제재 이력을 BB봇 인수인계 원장으로 가져온다."""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
from pathlib import Path
from typing import Any

import database


TYPE_MAP = {"warn": "WARNING", "note": "NOTE", "ban": "BAN"}
MAX_IMPORT_BYTES = 10 * 1024 * 1024


def _timestamp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _profile_display(profiles: dict, guild_id: str, user_id: str,
                     fallback: str) -> str:
    profile = profiles.get(f"{guild_id}:{user_id}") or {}
    return str(profile.get("tag") or profile.get("username") or fallback).strip()[:120]


def prepare_records(payload: dict, guild_id: int) -> tuple[list[dict], dict[str, int]]:
    if payload.get("schemaVersion") != 1:
        raise ValueError("지원하지 않는 moderation-data 스키마 버전입니다.")
    sanctions = payload.get("sanctions")
    profiles = payload.get("memberProfiles") or {}
    if not isinstance(sanctions, list) or not isinstance(profiles, dict):
        raise ValueError("sanctions/memberProfiles 구조가 올바르지 않습니다.")

    target_guild = str(int(guild_id))
    prepared: list[dict] = []
    counts = {"WARNING": 0, "NOTE": 0, "BAN": 0, "excluded_other_guild": 0}
    seen_ids: set[str] = set()
    for item in sanctions:
        if not isinstance(item, dict):
            raise ValueError("sanctions 배열에 객체가 아닌 값이 있습니다.")
        if str(item.get("guildId") or "") != target_guild:
            counts["excluded_other_guild"] += 1
            continue
        legacy_id = str(item.get("id") or "").strip()
        if not legacy_id or legacy_id in seen_ids:
            raise ValueError("제재 ID가 비어 있거나 중복되었습니다.")
        seen_ids.add(legacy_id)
        source_type = str(item.get("type") or "").lower()
        action_type = TYPE_MAP.get(source_type)
        if action_type is None:
            raise ValueError(f"지원하지 않는 제재 유형: {source_type[:32]}")
        user_id = int(str(item.get("targetUserId") or "0"))
        moderator_id = int(str(item.get("moderatorId") or "0"))
        if user_id <= 0 or moderator_id <= 0:
            raise ValueError("대상 또는 처리자 Discord ID가 올바르지 않습니다.")
        issued_at = _timestamp(item.get("createdAt"))
        if issued_at is None:
            raise ValueError("제재 적용 시각이 없습니다.")
        expires_at = _timestamp(item.get("expiresAt"))
        released_at = _timestamp(item.get("releasedAt"))
        is_active = item.get("active") is True
        if source_type == "note":
            status = "expired"
            released_at = released_at or expires_at or issued_at
            release_reason = str(
                item.get("releaseReason") or "과거 운영 메모 보존 기간 종료"
            )
        elif is_active:
            status = "active"
            release_reason = None
        else:
            status = "released"
            released_at = released_at or expires_at or issued_at
            release_reason = str(item.get("releaseReason") or "과거 기록에서 종료됨")

        target_fallback = str(item.get("targetTag") or f"Discord 사용자 {user_id}")
        prepared.append({
            "guild_id": int(guild_id),
            "user_id": user_id,
            "user_display": _profile_display(
                profiles, target_guild, str(user_id), target_fallback
            ),
            "action_type": action_type,
            "reason": str(item.get("reason") or "사유 미입력"),
            "dedupe_key": f"legacy-moderation:{legacy_id}",
            "status": status,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "released_at": released_at,
            "issued_by_id": moderator_id,
            "issued_by_display": _profile_display(
                profiles, target_guild, str(moderator_id),
                f"Discord 관리자 {moderator_id}",
            ),
            "release_reason": release_reason,
        })
        counts[action_type] += 1
    return prepared, counts


async def run_import(path: Path, guild_id: int, *, dry_run: bool) -> dict[str, int]:
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("가져오기 파일이 10MB를 초과합니다.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    records, counts = prepare_records(payload, guild_id)
    counts.update({"inserted": 0, "duplicates": 0})
    if dry_run:
        return counts
    for record in records:
        _sanction_id, inserted = await database.import_sanction_record(**record)
        counts["inserted" if inserted else "duplicates"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="기존 제재 이력 JSON 가져오기")
    parser.add_argument("path", type=Path)
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    counts = asyncio.run(run_import(args.path.resolve(), args.guild_id, dry_run=args.dry_run))
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
