"""Discord 및 AI 공급자의 인증·모델 가용성을 변경 작업 없이 확인한다."""

import asyncio
import os
import sys
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

import config


load_dotenv()


def _configured_discord_channels() -> list[tuple[str, int]]:
    """ID 자체를 출력하지 않도록 용도 라벨과 설정된 채널 ID를 반환한다."""
    channels = []
    for label, name in (
        ("log", "LOG_CHANNEL_ID"),
        ("public log", "PUBLIC_LOG_CHANNEL_ID"),
        ("report", "REPORT_CHANNEL_ID"),
    ):
        raw = os.environ.get(name, "").strip()
        if raw:
            channels.append((label, int(raw)))
    channels.extend(
        (f"watched {index}", channel_id)
        for index, channel_id in enumerate(config.WATCHED_CHANNEL_IDS, 1)
    )
    return channels


async def _check_response(client: httpx.AsyncClient, name: str, url: str, headers: dict) -> str:
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return f"[OK] {name}"
    except httpx.HTTPStatusError as error:
        return f"[FAIL] {name}: HTTP {error.response.status_code}"
    except httpx.HTTPError as error:
        return f"[FAIL] {name}: {type(error).__name__}"


async def _check_groq_model(client: httpx.AsyncClient, api_key: str, model: str) -> str:
    """슬래시가 포함된 Groq 모델 ID는 목록 API에서 정확히 일치하는지 확인한다."""
    name = f"Groq model {model}"
    try:
        response = await client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        model_ids = {item.get("id") for item in response.json().get("data", [])}
        return f"[OK] {name}" if model in model_ids else f"[FAIL] {name}: not found"
    except httpx.HTTPStatusError as error:
        return f"[FAIL] {name}: HTTP {error.response.status_code}"
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return f"[FAIL] {name}: {type(error).__name__}"


async def _check_ollama(client: httpx.AsyncClient, base_url: str, model: str) -> str:
    """
    실시간 3차 폴백용 로컬 Ollama가 떠 있고 모델이 받아져 있는지 확인한다.
    없어도 봇은 정상 동작(Gemini/Groq만 사용)하므로 실패가 아니라 [WARN]으로 알린다.
    """
    name = f"Ollama model {model}"
    try:
        response = await client.get(f"{base_url}/api/tags")
        response.raise_for_status()
        pulled = {item.get("name") for item in response.json().get("models", [])}
        if model in pulled or f"{model}:latest" in pulled:
            return f"[OK] {name}"
        return f"[WARN] {name}: not pulled (run: ollama pull {model})"
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return f"[WARN] {name}: {type(error).__name__} (3rd fallback unavailable)"


async def run_network_checks() -> int:
    """읽기 전용 상태 확인을 실행하고 하나라도 실패하면 1을 반환한다."""
    checks = []
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()

    async with httpx.AsyncClient(timeout=15) as client:
        if token:
            discord_headers = {"Authorization": f"Bot {token}"}
            checks.append(_check_response(
                client,
                "Discord bot token",
                "https://discord.com/api/v10/users/@me",
                discord_headers,
            ))
            for label, channel_id in _configured_discord_channels():
                checks.append(_check_response(
                    client,
                    f"Discord channel {label}",
                    f"https://discord.com/api/v10/channels/{channel_id}",
                    discord_headers,
                ))
        else:
            print("[FAIL] Discord bot token: DISCORD_BOT_TOKEN missing")

        if gemini_key:
            checks.append(_check_response(
                client,
                f"Gemini model {config.GEMINI_MODEL}",
                f"https://generativelanguage.googleapis.com/v1beta/models/{quote(config.GEMINI_MODEL, safe='')}",
                {"x-goog-api-key": gemini_key},
            ))
        else:
            print("[SKIP] Gemini: GEMINI_API_KEY missing")

        if groq_key:
            checks.append(_check_groq_model(client, groq_key, config.GROQ_MODEL))
        else:
            print("[SKIP] Groq: GROQ_API_KEY missing")

        if config.OLLAMA_REALTIME_FALLBACK:
            checks.append(_check_ollama(client, config.OLLAMA_BASE_URL, config.OLLAMA_MODEL))
        else:
            print("[SKIP] Ollama: OLLAMA_REALTIME_FALLBACK disabled")

        results = await asyncio.gather(*checks)

    for result in results:
        print(result)
    has_failure = any(result.startswith("[FAIL]") for result in results)
    return 1 if has_failure or not token else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_network_checks()))
