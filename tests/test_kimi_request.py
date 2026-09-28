"""The K2.6 request is serialized correctly without contacting the API."""

import json
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from backend.config import settings
from backend.translation import moonshot


class KimiRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_request_uses_available_model_without_thinking(self):
        captured = {}

        def respond(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            body = (
                'data: {"id":"test","object":"chat.completion.chunk","created":0,'
                '"model":"kimi-k2.6","choices":[{"index":0,'
                '"delta":{"content":"你好"},"finish_reason":null}]}\n\n'
                'data: [DONE]\n\n'
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        client = AsyncOpenAI(
            api_key="local-test-key", base_url="https://api.moonshot.cn/v1",
            http_client=http_client,
        )
        try:
            with patch.object(settings, "moonshot_model", "kimi-k2.6"), \
                 patch.object(moonshot, "get_client", return_value=client):
                output = [delta async for delta in moonshot.translate_stream("안녕하세요")]
        finally:
            await client.close()

        self.assertEqual(output, ["你好"])
        self.assertEqual(captured["model"], "kimi-k2.6")
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertTrue(captured["stream"])
        self.assertNotIn("temperature", captured)


if __name__ == "__main__":
    unittest.main()
