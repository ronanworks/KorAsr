"""Shared Moonshot request budget and the 429 retry of live translation; no API is contacted."""

import asyncio
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

os.environ.setdefault("MOONSHOT_API_KEY", "test-key")
from backend.config import settings  # noqa: E402
from backend.polish import translator  # noqa: E402
from backend.summary import generator  # noqa: E402
from backend.translation import moonshot, ratelimit  # noqa: E402
from backend.translation.ratelimit import RequestRateLimiter, rate_limit_wait_s  # noqa: E402

STREAM_OK = (
    'data: {"id":"t","object":"chat.completion.chunk","created":0,"model":"kimi-k2.6",'
    '"choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}\n\n'
    "data: [DONE]\n\n"
)


def _rate_limited(after: str = "0.05") -> httpx.Response:
    message = (f"Your account org-test<ak-test> request reached organization max RPM: 100, "
               f"please try again after {after} seconds")
    return httpx.Response(429, json={"error": {"message": message, "type": "rate_limit_reached_error"}})


class _Server:
    """httpx transport answering with fresh responses from the given factories, in order (then the last one)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return response()

    def client(self) -> AsyncOpenAI:
        # As in production (moonshot.get_client): the SDK itself never retries.
        return AsyncOpenAI(api_key="local-test-key", base_url="https://api.moonshot.cn/v1",
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(self)), max_retries=0)


def _ok():
    return httpx.Response(200, text=STREAM_OK, headers={"content-type": "text/event-stream"})


class LiveTranslationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def _translate(self, server):
        client = server.client()
        try:
            with patch.object(moonshot, "get_client", return_value=client), \
                 patch.object(settings, "moonshot_max_rpm", 60):
                return [delta async for delta in moonshot.translate_stream("안녕하세요")]
        finally:
            await client.close()

    async def test_two_429s_are_retried_after_the_named_wait(self):
        server = _Server(_rate_limited, _rate_limited, _ok)
        started = time.perf_counter()
        output = await self._translate(server)
        self.assertEqual(output, ["你好"])
        self.assertEqual(len(server.requests), 3)
        self.assertGreaterEqual(time.perf_counter() - started, 0.1)   # waited 2 × 0.05 s
        self.assertEqual(server.requests[0], server.requests[2])       # the same request again

    async def test_a_third_429_fails_the_translation(self):
        server = _Server(_rate_limited)
        with self.assertRaises(Exception) as caught:
            await self._translate(server)
        self.assertEqual(getattr(caught.exception, "status_code", None), 429)
        self.assertEqual(len(server.requests), 3)   # first try + 2 retries

    async def test_other_errors_are_not_retried(self):
        server = _Server(lambda: httpx.Response(500, json={"error": {"message": "boom"}}))
        with self.assertRaises(Exception):
            await self._translate(server)
        self.assertEqual(len(server.requests), 1)

    async def test_every_attempt_takes_a_slot_of_the_shared_budget(self):
        labels = []

        async def slot(label="request"):
            labels.append(label)
            return 0.0

        server = _Server(_rate_limited, _ok)
        with patch.object(moonshot, "acquire_moonshot_slot", slot):
            await self._translate(server)
        self.assertEqual(labels, ["live translation", "live translation"])


class _Err(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers=headers or {})


class RetryAfterTests(unittest.TestCase):
    def test_wait_comes_from_the_message_then_the_header_then_2_s(self):
        self.assertEqual(rate_limit_wait_s(_Err(429, "Error code: 429 - {'error': {'message': "
                                                     "'... max RPM: 100, please try again after 1 seconds'}}")), 1.0)
        self.assertEqual(rate_limit_wait_s(_Err(429, "slow down", {"retry-after": "3"})), 3.0)
        self.assertEqual(rate_limit_wait_s(_Err(429, "slow down")), 2.0)
        self.assertEqual(rate_limit_wait_s(_Err(429, "try again after 600 seconds")), ratelimit.RATE_LIMIT_MAX_WAIT_S)
        self.assertIsNone(rate_limit_wait_s(_Err(500, "please try again after 1 seconds")))
        self.assertIsNone(rate_limit_wait_s(RuntimeError("stream interrupted")))


class LimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_beyond_the_limit_wait_for_the_window(self):
        limiter = RequestRateLimiter(2, window_s=0.2)
        self.assertEqual([await limiter.acquire(), await limiter.acquire()], [0.0, 0.0])
        started = time.perf_counter()
        await limiter.acquire()
        self.assertGreaterEqual(time.perf_counter() - started, 0.18)

    async def test_a_cancelled_wait_leaves_no_slot_behind(self):
        limiter = RequestRateLimiter(1, window_s=0.3)
        await limiter.acquire()
        waiting = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0.05)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(limiter.in_window(), 1)
        await asyncio.sleep(0.3)
        self.assertEqual(limiter.in_window(), 0)

    async def test_each_request_waits_on_its_own(self):
        # One request sleeping (e.g. a 429 back-off) holds nothing: the others go at once.
        limiter = RequestRateLimiter(3, window_s=60)

        async def backing_off():
            await limiter.acquire("a")
            await asyncio.sleep(0.3)
            await limiter.acquire("a")

        slow = asyncio.create_task(backing_off())
        await asyncio.sleep(0.01)
        started = time.perf_counter()
        await limiter.acquire("b")
        self.assertLess(time.perf_counter() - started, 0.05)
        await slow
        self.assertEqual(limiter.in_window(), 3)

    async def test_concurrent_requests_never_overbook(self):
        limiter = RequestRateLimiter(5, window_s=0.25)
        granted = []

        async def one(i):
            await limiter.acquire()
            granted.append(time.perf_counter())

        started = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(12)))
        offsets = sorted(t - started for t in granted)
        self.assertEqual(sum(offset < 0.2 for offset in offsets), 5)          # first window
        self.assertEqual(sum(offset < 0.45 for offset in offsets), 10)        # second window
        self.assertGreaterEqual(offsets[-1], 0.45)

    async def test_zero_means_no_limit_and_the_shared_limiter_follows_settings(self):
        limiter = RequestRateLimiter(0)
        for _ in range(500):
            await limiter.acquire()
        with patch.object(settings, "moonshot_max_rpm", 7):
            self.assertEqual(ratelimit.get_limiter().max_requests, 7)
        self.assertEqual(ratelimit.get_limiter().max_requests, settings.moonshot_max_rpm)

    async def test_polish_outline_and_summary_share_the_budget(self):
        labels = []

        async def slot(label="request"):
            labels.append(label)
            return 0.0

        class Completions:
            async def create(self, **kwargs):
                if kwargs.get("stream"):
                    async def chunks():
                        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="- 要点"))])
                    return chunks()
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="## 主要内容"))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        with patch.object(translator, "acquire_moonshot_slot", slot), \
             patch.object(translator, "get_client", return_value=client), \
             patch.object(generator, "acquire_moonshot_slot", slot), \
             patch.object(generator, "get_client", return_value=client):
            await translator._complete_streaming([{"role": "user", "content": "x"}], 100, 5)
            await translator.update_outline([(0, 60000, "- 开场")], "", "zh")
            segment = SimpleNamespace(sequence=0, timestamp_ms=0, ko_text="안녕하세요", zh_text="你好",
                                      lang="ko", target_lang="zh", speaker=None)
            await generator.generate_summary([segment])
        self.assertEqual(labels, ["polish", "outline", "summary"])


if __name__ == "__main__":
    unittest.main()
