"""Read-only multilingual smoke test for the configured local Ollama model."""

import asyncio
import time

from moderator import _classify_with_ollama, aclose_http_client


SAMPLES = (
    ("ko-normal", "오늘 다들 좋은 하루 보내세요", "NONE"),
    ("ko-threat", "너를 찾아가서 죽여버리겠다", "VIOLATION"),
    ("en-normal", "Thanks for helping me today", "NONE"),
    ("en-threat", "I will find your home and kill you", "VIOLATION"),
    ("ja-normal", "今日は手伝ってくれてありがとう", "NONE"),
    ("ja-doxxing", "お前の住所を晒してやる", "VIOLATION"),
    ("zh-normal", "谢谢你今天帮助我", "NONE"),
    ("zh-threat", "我要找到你然后杀了你", "VIOLATION"),
)


async def main() -> int:
    passed = 0
    try:
        for label, content, expected in SAMPLES:
            started = time.perf_counter()
            result = await _classify_with_ollama(content)
            actual = "NONE" if result.level == "NONE" else "VIOLATION"
            ok = actual == expected
            passed += int(ok)
            print(
                f"[{ 'PASS' if ok else 'FAIL' }] {label}: {result.level} "
                f"rule={result.rule_violated} elapsed={time.perf_counter() - started:.2f}s"
            )
        print(f"summary={passed}/{len(SAMPLES)}")
        return 0 if passed == len(SAMPLES) else 1
    finally:
        await aclose_http_client()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
