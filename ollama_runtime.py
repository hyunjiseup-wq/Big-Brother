"""봇 시작 시 로컬 Ollama 판단망을 안전하게 확인하고 필요하면 숨김 기동한다."""

import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

import config


def _tags_url() -> str:
    return f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/tags"


def _local_ollama_url() -> bool:
    host = (urlparse(config.OLLAMA_BASE_URL).hostname or "").casefold()
    return host in {"localhost", "127.0.0.1", "::1"}


def _available_models(timeout: float = 2.0) -> set[str] | None:
    try:
        response = httpx.get(_tags_url(), timeout=timeout)
        response.raise_for_status()
        return {
            str(item.get("name", ""))
            for item in response.json().get("models", [])
            if item.get("name")
        }
    except (httpx.HTTPError, ValueError, TypeError):
        return None


def _find_ollama_executable() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = (
        Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe",
        Path(local_app_data) / "AMD" / "AI_Bundle" / "Ollama" / "ollama.exe",
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def ensure_ollama_running() -> tuple[bool, str]:
    """(사용 가능 여부, 사용자용 상태 설명)을 반환한다."""
    if not config.OLLAMA_REALTIME_FALLBACK:
        return False, "로컬 폴백 비활성화"

    models = _available_models()
    if models is not None:
        if config.OLLAMA_MODEL in models:
            return True, f"Ollama 준비됨 ({config.OLLAMA_MODEL})"
        return False, f"Ollama 모델 없음: {config.OLLAMA_MODEL}"

    if not config.OLLAMA_AUTO_START or not _local_ollama_url():
        return False, "Ollama 연결 불가 (자동 시작 대상 아님)"

    executable = _find_ollama_executable()
    if not executable:
        return False, "Ollama 실행 파일을 찾지 못함"

    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([executable, "serve"], **kwargs)
    except OSError as error:
        return False, f"Ollama 자동 시작 실패: {type(error).__name__}"

    deadline = time.monotonic() + config.OLLAMA_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.5)
        models = _available_models()
        if models is not None:
            if config.OLLAMA_MODEL in models:
                return True, f"Ollama 자동 시작 완료 ({config.OLLAMA_MODEL})"
            return False, f"Ollama 자동 시작됨, 모델 없음: {config.OLLAMA_MODEL}"
    return False, f"Ollama 자동 시작 시간 초과 ({config.OLLAMA_STARTUP_TIMEOUT_SECONDS}초)"
