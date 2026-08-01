import unittest
import os
from unittest.mock import patch

import httpx

import network_check


class NetworkCheckTests(unittest.IsolatedAsyncioTestCase):
    def test_configured_discord_channels_use_labels_without_exposing_ids(self):
        values = {
            "LOG_CHANNEL_ID": "101",
            "PUBLIC_LOG_CHANNEL_ID": "102",
            "REPORT_CHANNEL_ID": "",
        }
        with (
            patch.dict(os.environ, values, clear=True),
            patch.object(network_check.config, "WATCHED_CHANNEL_IDS", [201, 202]),
        ):
            channels = network_check._configured_discord_channels()
        self.assertEqual(channels, [("log", 101), ("public log", 102), ("watched 1", 201), ("watched 2", 202)])

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
