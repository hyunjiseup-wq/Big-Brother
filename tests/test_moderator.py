import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import moderator


class ModeratorBatchTests(unittest.IsolatedAsyncioTestCase):
    def test_batch_parser_rejects_non_object_items(self):
        with self.assertRaises(ValueError):
            moderator._parse_batch_json('[{"index": 0}, "bad"]')

    async def test_incomplete_batch_response_is_a_failure(self):
        messages = [
            {"index": 0, "author_ref": "user_1", "content": "a"},
            {"index": 1, "author_ref": "user_2", "content": "b"},
        ]
        response = [{"index": 0, "level": "NONE", "rule_violated": "NONE", "reason": ""}]
        with patch.object(
            moderator, "_classify_batch_with_gemini", new=AsyncMock(return_value=response)
        ):
            with self.assertRaises(moderator.BatchClassificationError):
                await moderator.classify_batch(messages, backend="gemini")


class RealtimeOllamaFallbackTests(unittest.IsolatedAsyncioTestCase):
    """Gemini/Groq 무료 한도가 함께 마르면 로컬 Ollama가 마지막 그물이 되어야 한다."""

    def setUp(self):
        # 회로 차단기는 모듈 전역 상태라 테스트 간에 새 나가지 않도록 매번 초기화한다.
        moderator.reset_ollama_breaker()
        self.addCleanup(moderator.reset_ollama_breaker)
        cloud_down = AsyncMock(side_effect=RuntimeError("quota exceeded"))
        for name in ("_classify_with_gemini", "_classify_with_groq"):
            patcher = patch.object(moderator, name, new=cloud_down)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_ollama_judges_when_both_free_apis_are_exhausted(self):
        verdict = moderator.ModerationResult("MODERATE", "3", "반말", provider="ollama")
        with patch.object(moderator, "_classify_with_ollama",
                          new=AsyncMock(return_value=verdict)) as ollama:
            result = await moderator.classify_message("뭐함")
        ollama.assert_awaited_once()
        self.assertEqual(result.provider, "ollama")
        self.assertEqual(result.level, "MODERATE")

    async def test_disabled_fallback_reports_failure_without_calling_ollama(self):
        with (
            patch.object(moderator, "OLLAMA_REALTIME_FALLBACK", False),
            patch.object(moderator, "_classify_with_ollama", new=AsyncMock()) as ollama,
        ):
            result = await moderator.classify_message("뭐함")
        ollama.assert_not_awaited()
        self.assertEqual(result.provider, "none")
        self.assertEqual(result.level, "NONE")

    async def test_all_three_failing_is_still_a_safe_none(self):
        with patch.object(moderator, "_classify_with_ollama",
                          new=AsyncMock(side_effect=RuntimeError("model missing"))):
            result = await moderator.classify_message("뭐함")
        self.assertEqual(result.provider, "none")
        self.assertEqual(result.level, "NONE")

    async def test_connection_failure_stops_retrying_for_the_cooldown(self):
        """Ollama가 안 떠 있는 PC에서 메시지마다 연결을 시도하다 큐가 밀리면 안 된다."""
        with (
            patch.object(moderator, "OLLAMA_UNAVAILABLE_COOLDOWN_SECONDS", 300),
            patch.object(moderator, "_classify_with_ollama",
                         new=AsyncMock(side_effect=httpx.ConnectError("refused"))) as ollama,
        ):
            await moderator.classify_message("첫 번째")
            await moderator.classify_message("두 번째")
        self.assertEqual(ollama.await_count, 1)
        ready, remaining = moderator.ollama_fallback_status()
        self.assertFalse(ready)
        self.assertGreater(remaining, 0)

    async def test_timeout_is_treated_as_transient_and_retried(self):
        """로컬 추론은 원래 느리다 — 한 번 늦었다고 마지막 그물을 걷어내면 안 된다."""
        with (
            patch.object(moderator, "OLLAMA_UNAVAILABLE_COOLDOWN_SECONDS", 300),
            patch.object(moderator, "_classify_with_ollama",
                         new=AsyncMock(side_effect=httpx.ReadTimeout("slow"))) as ollama,
        ):
            await moderator.classify_message("첫 번째")
            await moderator.classify_message("두 번째")
        self.assertEqual(ollama.await_count, 2)
        self.assertTrue(moderator.ollama_fallback_status()[0])

    async def test_waiting_for_a_turn_counts_against_the_time_budget(self):
        """세마포어 앞에 줄 서느라 워커가 제한 시간의 몇 배를 붙잡히면 큐가 밀린다."""
        async def never_returns(url, **kwargs):
            await asyncio.sleep(3600)

        client = SimpleNamespace(post=never_returns)
        blocked = asyncio.Semaphore(1)
        await blocked.acquire()  # 앞선 호출이 이미 자리를 차지한 상황

        with (
            patch.object(moderator, "OLLAMA_REALTIME_TIMEOUT_SECONDS", 0.05),
            patch.object(moderator, "_ollama_semaphore", blocked),
            patch.object(moderator, "_get_http_client", return_value=client),
        ):
            result = await moderator.classify_message("뭐함")
        self.assertEqual(result.provider, "none")
        # 메시지가 없는 예외라도 사유에 종류는 남아야 관리자가 원인을 추적할 수 있다
        self.assertIn("TimeoutError", result.reason)
        # 일시적 지연이므로 마지막 그물을 걷어내면 안 된다
        self.assertTrue(moderator.ollama_fallback_status()[0])

    async def test_local_calls_stay_within_their_own_concurrency_limit(self):
        """GPU 한 대에 AI 워커 수만큼(기본 8) 동시에 밀어 넣으면 전부 느려진다."""
        live = 0
        peak = 0

        async def fake_post(url, **kwargs):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0)  # 다른 대기 중인 호출에 실행 기회를 준다
            live -= 1
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={"message": {"content": '{"level": "NONE", "rule_violated": "NONE",'
                                             ' "reason": ""}'}},
            )

        client = SimpleNamespace(post=fake_post)
        with (
            patch.object(moderator, "_ollama_semaphore", asyncio.Semaphore(1)),
            patch.object(moderator, "_get_http_client", return_value=client),
        ):
            results = await asyncio.gather(
                *(moderator.classify_message(f"메시지 {i}") for i in range(5))
            )
        self.assertEqual(peak, 1)
        self.assertTrue(all(result.provider == "ollama" for result in results))


if __name__ == "__main__":
    unittest.main()
