"""Load a community moderation policy from a small validated JSON file."""

import json
import os
from pathlib import Path


def _channel_key(value: str) -> int | str:
    stripped = value.strip()
    if stripped.isdecimal():
        return int(stripped)
    if not stripped:
        raise ValueError("channel_context_notes에 빈 채널 키를 사용할 수 없습니다.")
    return stripped


def load_policy_file(path: str | os.PathLike) -> tuple[str, dict[int | str, str]]:
    resolved = Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"POLICY_FILE을 찾을 수 없습니다: {resolved}") from error
    except json.JSONDecodeError as error:
        raise ValueError(
            f"POLICY_FILE JSON 오류 ({resolved.name}:{error.lineno}:{error.colno})"
        ) from error

    if not isinstance(data, dict):
        raise ValueError("POLICY_FILE 최상위 값은 JSON 객체여야 합니다.")
    rules = data.get("server_rules")
    notes = data.get("channel_context_notes", {})
    if not isinstance(rules, str) or not rules.strip():
        raise ValueError("POLICY_FILE의 server_rules는 비어 있지 않은 문자열이어야 합니다.")
    if not isinstance(notes, dict):
        raise ValueError("POLICY_FILE의 channel_context_notes는 JSON 객체여야 합니다.")

    normalized = {}
    for key, note in notes.items():
        if not isinstance(note, str) or not note.strip():
            raise ValueError(f"채널 {key!r}의 특수 규칙은 비어 있지 않은 문자열이어야 합니다.")
        normalized[_channel_key(str(key))] = note.strip()
    return rules.strip(), normalized
