"""Per-IP rate limiting.

A public endpoint with no throttle is an invitation: to cost (each question can
be an LLM call), to noise in the audit log, and to trivial denial of service.

Deliberately in-process and dependency-free. That means the limit is per worker,
not per cluster - honest about what it is. For multi-instance deployments put a
real limiter in the gateway; this stops the obvious abuse of a single public
instance, which is the situation it exists for.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque


class RateLimiter:
    """Sliding-window counter, capped so it cannot grow without bound."""

    def __init__(self, per_minute: int, max_clients: int = 10_000):
        self.per_minute = per_minute
        self.max_clients = max_clients
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0

    def check(self, client: str, now: float | None = None) -> tuple[bool, int, int]:
        """Return (allowed, remaining, retry_after_seconds)."""
        if not self.enabled:
            return True, -1, 0
        now = now if now is not None else time.monotonic()
        window_start = now - 60.0

        with self._lock:
            hits = self._hits.get(client)
            if hits is None:
                hits = deque()
                self._hits[client] = hits
            self._hits.move_to_end(client)

            while hits and hits[0] < window_start:
                hits.popleft()

            if len(hits) >= self.per_minute:
                retry_after = max(1, int(60 - (now - hits[0])) + 1)
                return False, 0, retry_after

            hits.append(now)

            # Evict the least recently seen clients rather than leak memory.
            while len(self._hits) > self.max_clients:
                self._hits.popitem(last=False)

            return True, self.per_minute - len(hits), 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
