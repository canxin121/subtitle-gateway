import asyncio
import unittest
from unittest.mock import patch

import httpx

import gateway.config as config
from gateway.config import RuntimeConfig
from gateway import server


class OpenAIAuthTests(unittest.TestCase):
    def setUp(self):
        self.original_config = config.CURRENT
        config.CURRENT = RuntimeConfig()

    def tearDown(self):
        config.CURRENT = self.original_config

    async def _send(self, method, path, **kwargs):
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(method, path, **kwargs)

    def request(self, method, path, **kwargs):
        return asyncio.run(self._send(method, path, **kwargs))

    def transcription_request(self, headers=None):
        return self.request(
            "POST",
            "/v1/audio/transcriptions",
            headers=headers,
            files={"file": ("audio.wav", b"audio", "audio/wav")},
            data={"model": "sensevoice"},
        )

    def test_models_remain_unauthenticated_when_key_is_not_configured(self):
        response = self.request("GET", "/v1/models")
        self.assertEqual(response.status_code, 200)

    def test_transcription_remains_unauthenticated_when_key_is_not_configured(self):
        with patch.object(
            server.asr, "run_transcription", return_value=("hello", [], 0.01)
        ):
            response = self.transcription_request()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"text": "hello"})

    def test_models_require_valid_bearer_key_when_configured(self):
        config.CURRENT.openai_api_key = "test-key"

        for headers in (
            None,
            {"Authorization": "Basic test-key"},
            {"Authorization": "Bearer wrong-key"},
        ):
            with self.subTest(headers=headers):
                response = self.request("GET", "/v1/models", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")

        response = self.request(
            "GET", "/v1/models", headers={"Authorization": "Bearer test-key"}
        )
        self.assertEqual(response.status_code, 200)

    def test_transcription_rejects_missing_key_before_inference(self):
        config.CURRENT.openai_api_key = "test-key"

        with patch.object(server.asr, "run_transcription") as run_transcription:
            response = self.transcription_request()

        self.assertEqual(response.status_code, 401)
        run_transcription.assert_not_called()

    def test_transcription_accepts_valid_key(self):
        config.CURRENT.openai_api_key = "test-key"

        with patch.object(
            server.asr, "run_transcription", return_value=("hello", [], 0.01)
        ):
            response = self.transcription_request(
                headers={"Authorization": "Bearer test-key"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"text": "hello"})

    def test_openai_key_does_not_protect_health_endpoint(self):
        config.CURRENT.openai_api_key = "test-key"

        with patch.object(server.asr, "accelerator_memory_stats", return_value={}):
            response = self.request("GET", "/health")

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
