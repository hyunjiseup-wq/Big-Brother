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
