import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import vision


PNG = b"\x89PNG\r\n\x1a\n" + b"test-image-data"


def _attachment(data=PNG, *, filename="overall.png", content_type="image/png",
                size=None, attachment_id=1):
    return SimpleNamespace(
        id=attachment_id,
        filename=filename,
        content_type=content_type,
        size=len(data) if size is None else size,
        read=AsyncMock(return_value=data),
    )


def _message(*attachments, channel_id=1445049743150415923, channel_name="핵의심-신고"):
    channel = SimpleNamespace(
        id=channel_id, parent_id=None, parent=None, name=channel_name,
    )
    return SimpleNamespace(channel=channel, attachments=list(attachments))


class VisionAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        vision._analysis_cache.clear()
        vision.reset_provider_cooldowns()

    def test_only_configured_report_channel_is_targeted(self):
        self.assertTrue(vision.is_vision_channel(_message().channel))
        self.assertFalse(vision.is_vision_channel(
            _message(channel_id=999, channel_name="일반").channel
        ))
        thread = SimpleNamespace(
            id=888,
            parent_id=1445049743150415923,
            parent=SimpleNamespace(id=1445049743150415923, name="핵의심 신고"),
            name="신고 글",
        )
        self.assertTrue(vision.is_vision_channel(thread))

    def test_supported_image_attachment_is_detected(self):
        message = _message(_attachment())
        self.assertTrue(vision.has_image_attachments(message))
        self.assertEqual(vision.attachment_fingerprint(message), ((1, "overall.png", len(PNG)),))

    def test_image_only_false_positive_has_nonempty_learning_evidence(self):
        visual = {
            "status": "analyzed",
            "provider": "ollama",
            "analyzed_images": 1,
            "image_kind": "overall",
            "game_nicknames": ["Suspect"],
            "raid_servers": ["Seoul"],
            "maps": ["Customs"],
            "ocr_text": "Nickname Suspect",
            "observations": ["오버롤 화면"],
        }
        evidence = vision.learning_evidence("", visual)
        self.assertIn("첨부 이미지 자동 분석", evidence)
        self.assertIn("Suspect", evidence)
        self.assertIn("핵 사용의 확정 증거가 아님", evidence)

    async def test_fake_image_extension_is_not_sent_to_provider(self):
        message = _message(_attachment(b"not-an-image", content_type=None))
        with patch.object(vision, "_analyze_with_ollama", new=AsyncMock()) as analyze:
            result = await vision.analyze_message_attachments(message)
        self.assertEqual(result["status"], "skipped")
        analyze.assert_not_awaited()

    async def test_oversized_image_is_skipped_before_download(self):
        attachment = _attachment(size=vision.config.VISION_MAX_IMAGE_BYTES + 1)
        result = await vision.analyze_message_attachments(_message(attachment))
        self.assertEqual(result["status"], "skipped")
        attachment.read.assert_not_awaited()

    async def test_provider_fallback_normalizes_ocr_result(self):
        data = {
            "ocr_text": "Nickname: Suspect",
            "game_nicknames": ["Suspect"],
            "raid_servers": ["Seoul"],
            "maps": ["Customs"],
            "image_kind": "overall",
            "observations": ["오버롤 화면"],
            "contains_real_personal_info": False,
            "contains_cheat_promotion_or_sale": False,
            "confidence": "high",
        }
        with (
            patch.object(vision.config, "VISION_PROVIDER_ORDER", ("ollama", "gemini")),
            patch.object(
                vision, "_analyze_with_ollama",
                new=AsyncMock(side_effect=RuntimeError("model missing")),
            ),
            patch.object(
                vision, "_analyze_with_gemini", new=AsyncMock(return_value=data)
            ) as gemini,
        ):
            result = await vision.analyze_message_attachments(_message(_attachment()))
        gemini.assert_awaited_once()
        self.assertEqual(result["provider"], "gemini")
        self.assertEqual(result["game_nicknames"], ["Suspect"])
        self.assertEqual(result["image_kind"], "overall")
        self.assertIn("확정 증거가 아님", result["warning"])

    async def test_successful_analysis_is_cached_for_retry(self):
        data = {"ocr_text": "cached", "image_kind": "overall", "confidence": "medium"}
        analyzer = AsyncMock(return_value=data)
        message = _message(_attachment())
        with (
            patch.object(vision.config, "VISION_PROVIDER_ORDER", ("ollama",)),
            patch.object(vision, "_analyze_with_ollama", new=analyzer),
        ):
            first = await vision.analyze_message_attachments(message)
            second = await vision.analyze_message_attachments(message)
        self.assertEqual(first, second)
        analyzer.assert_awaited_once()

    async def test_all_provider_failure_requires_retry(self):
        failure = AsyncMock(side_effect=RuntimeError("unavailable"))
        with (
            patch.object(vision.config, "VISION_PROVIDER_ORDER", ("ollama",)),
            patch.object(vision, "_analyze_with_ollama", new=failure),
        ):
            with self.assertRaisesRegex(vision.VisionAnalysisUnavailable, "ollama"):
                await vision.analyze_message_attachments(
                    _message(_attachment(PNG + b"unique-failure"))
                )


class VisionProviderPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_ollama_accepts_structured_json_returned_in_thinking_field(self):
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {
                "content": "",
                "thinking": json.dumps({
                    "ocr_text": "닉네임 Suspect",
                    "image_kind": "overall",
                    "confidence": "high",
                }),
            }},
        )
        client = SimpleNamespace(post=AsyncMock(return_value=response))
        with patch.object(vision, "_get_http_client", return_value=client):
            result = await vision._analyze_with_ollama([
                vision.ImageInput("overall.png", "image/png", PNG)
            ])
        self.assertEqual(result["ocr_text"], "닉네임 Suspect")
        self.assertEqual(result["image_kind"], "overall")

    async def test_gemini_uses_inline_image_data(self):
        response = SimpleNamespace(json=lambda: {
            "candidates": [{"content": {"parts": [{"text": json.dumps({
                "ocr_text": "ok", "image_kind": "overall", "confidence": "high"
            })}]}}]
        })
        with (
            patch.object(vision, "GEMINI_API_KEY", "test-key"),
            patch.object(vision, "_post_with_retry", new=AsyncMock(return_value=response)) as post,
        ):
            await vision._analyze_with_gemini([
                vision.ImageInput("overall.png", "image/png", PNG)
            ])
        payload = post.await_args.kwargs["json"]
        image_part = payload["contents"][0]["parts"][1]["inline_data"]
        self.assertEqual(image_part["mime_type"], "image/png")
        self.assertTrue(image_part["data"])
        self.assertNotIn("test-key", str(payload))

    async def test_groq_uses_data_url_and_vision_model(self):
        response = SimpleNamespace(json=lambda: {
            "choices": [{"message": {"content": json.dumps({
                "ocr_text": "ok", "image_kind": "raid", "confidence": "medium"
            })}}]
        })
        with (
            patch.object(vision, "GROQ_API_KEY", "test-key"),
            patch.object(vision, "_post_with_retry", new=AsyncMock(return_value=response)) as post,
        ):
            await vision._analyze_with_groq([
                vision.ImageInput("raid.png", "image/png", PNG)
            ])
        payload = post.await_args.kwargs["json"]
        self.assertEqual(payload["model"], vision.config.GROQ_VISION_MODEL)
        self.assertTrue(
            payload["messages"][0]["content"][1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
        )
