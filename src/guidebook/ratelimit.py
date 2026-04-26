"""In-memory per-IP sliding window rate limiter."""

import time
from collections import deque


class RateLimiter:
    """Sliding window rate limiter.

    Tracks timestamps of events per key (typically client IP) and rejects
    new events when the count within the window exceeds the maximum.
    """

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._last_cleanup = time.monotonic()

    def _cleanup(self) -> None:
        """Remove stale entries every 60 seconds."""
        now = time.monotonic()
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        cutoff = now - self.window_seconds
        stale = [k for k, dq in self._hits.items() if not dq or dq[-1] < cutoff]
        for k in stale:
            del self._hits[k]

    def check(self, key: str) -> tuple[bool, int]:
        """Check if the key is within the rate limit.

        Returns (allowed, retry_after_seconds).
        """
        now = time.monotonic()
        self._cleanup()

        dq = self._hits.get(key)
        if dq is None:
            return True, 0

        cutoff = now - self.window_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) >= self.max_requests:
            retry_after = int(dq[0] - cutoff) + 1
            return False, max(retry_after, 1)

        return True, 0

    def record(self, key: str) -> None:
        """Record a hit for the given key."""
        now = time.monotonic()
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
        dq.append(now)


# Auth: 5 failed attempts per 5-minute window
auth_limiter = RateLimiter(max_requests=5, window_seconds=300)

# Query: 10 requests per minute
query_limiter = RateLimiter(max_requests=10, window_seconds=60)

# Upload: 5 uploads per minute
upload_limiter = RateLimiter(max_requests=5, window_seconds=60)
