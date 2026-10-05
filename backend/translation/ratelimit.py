# backend/translation/ratelimit.py
"""One request budget per minute for every Moonshot (Kimi) call of this server.

The account limit is per organisation: 100 requests per minute across all callers
(429 "request reached organization max RPM: 100"). Live translation (one request per
sentence and target language), the per-minute polish, the outline and the summary
share MOONSHOT_MAX_RPM (default 60), which leaves room for other users of the key.

Sliding 60 s window. Every request waits on its own: nothing is locked while it
waits, and a 429 back-off is slept outside the limiter, so one request that waits
long never holds up the others.
"""
import asyncio
import re
import time
from collections import deque

from backend.config import settings

WINDOW_S = 60.0
LOG_WAIT_S = 0.5             # waits shorter than this are not logged

# 429 handling for live translation: wait what the server asks, then retry.
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_DEFAULT_WAIT_S = 2.0
RATE_LIMIT_MAX_WAIT_S = 15.0  # a live subtitle is useless after this; the caller marks it failed
_RETRY_AFTER_RE = re.compile(r"after\s+(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b", re.IGNORECASE)


class RequestRateLimiter:
    """At most `max_requests` grants in any `window_s` seconds; <= 0 means no limit."""

    def __init__(self, max_requests: int, window_s: float = WINDOW_S, clock=time.monotonic, sleep=None):
        self.max_requests = int(max_requests)
        self.window_s = float(window_s)
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._granted: deque[float] = deque()

    def _expire(self, now: float) -> None:
        while self._granted and self._granted[0] <= now - self.window_s:
            self._granted.popleft()

    def in_window(self) -> int:
        self._expire(self._clock())
        return len(self._granted)

    async def acquire(self, label: str = "request") -> float:
        """Wait until a request may be sent; returns the seconds waited.

        Check and grant happen without an await in between, so concurrent callers
        never overbook; a caller cancelled while waiting leaves nothing behind.
        """
        if self.max_requests <= 0:
            return 0.0
        started = self._clock()
        while True:
            now = self._clock()
            self._expire(now)
            if len(self._granted) < self.max_requests:
                self._granted.append(now)
                waited = now - started
                if waited >= LOG_WAIT_S:
                    print(f"[RateLimit] {label} waited {waited:.1f}s for a Moonshot slot "
                          f"(limit {self.max_requests}/{self.window_s:.0f}s)")
                return waited
            await self._sleep(max(0.01, self._granted[0] + self.window_s - now))


_limiter: RequestRateLimiter | None = None


def get_limiter() -> RequestRateLimiter:
    """The process-wide limiter; rebuilt only if MOONSHOT_MAX_RPM changes (tests)."""
    global _limiter
    limit = int(settings.moonshot_max_rpm)
    if _limiter is None or _limiter.max_requests != limit:
        _limiter = RequestRateLimiter(limit)
    return _limiter


async def acquire_moonshot_slot(label: str = "request") -> float:
    return await get_limiter().acquire(label)


def rate_limit_wait_s(exc: BaseException) -> float | None:
    """Seconds to wait before retrying a 429; None when `exc` is not a rate limit.

    Moonshot says "... please try again after 1 seconds" in the error message; a
    Retry-After header is used when there is no such text; else 2 s.
    """
    if getattr(exc, "status_code", None) != 429:
        return None
    wait = None
    match = _RETRY_AFTER_RE.search(str(exc))
    if match:
        wait = float(match.group(1))
    else:
        response = getattr(exc, "response", None)
        header = getattr(response, "headers", {}).get("retry-after") if response is not None else None
        try:
            wait = float(header) if header is not None else None
        except (TypeError, ValueError):
            wait = None
    if wait is None:
        wait = RATE_LIMIT_DEFAULT_WAIT_S
    return min(max(wait, 0.0), RATE_LIMIT_MAX_WAIT_S)
