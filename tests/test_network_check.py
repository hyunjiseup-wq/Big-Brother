import unittest

import httpx

import network_check


class NetworkCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_response(self):
        async def handler(request):
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await network_check._check_response(
                client, "service", "https://example.test/status", {"Authorization": "secret"}
            )
        self.assertEqual(result, "[OK] service")

    async def test_http_failure_reports_only_status(self):
        async def handler(request):
            return httpx.Response(401, text="secret-token-was-invalid")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await network_check._check_response(
                client, "service", "https://example.test/status", {"Authorization": "secret"}
            )
        self.assertEqual(result, "[FAIL] service: HTTP 401")
        self.assertNotIn("secret", result)

    async def test_groq_model_is_checked_from_model_list(self):
        async def handler(request):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await network_check._check_groq_model(
                client, "secret", "openai/gpt-oss-120b"
            )
        self.assertEqual(result, "[OK] Groq model openai/gpt-oss-120b")

    async def test_missing_groq_model_is_reported(self):
        async def handler(request):
            return httpx.Response(200, json={"data": [{"id": "another-model"}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await network_check._check_groq_model(
                client, "secret", "openai/gpt-oss-120b"
            )
        self.assertEqual(result, "[FAIL] Groq model openai/gpt-oss-120b: not found")
