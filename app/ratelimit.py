"""In-process, per-key fixed-window rate limiter.

Deliberately dependency-free and in-process. At this system's scale (a single
API process serving ~1-50 requests) a per-process limiter is exactly right: it
needs no Redis round-trip on the hot path and no extra dependency. The tradeoff
is honest and documented — with multiple API replicas each would hold its own
counters, so the effective global limit is ``limit * replicas``. Moving the
counters to Redis (keyed the same way) is the standard fix when horizontal
scaling arrives; it is out of scope here.

Algorithm: fixed window. Each key gets ``limit`` requests per ``window_s``;
when the window elapses the counter resets. Fixed windows can admit up to
``2 * limit`` requests across a window boundary (the classic burst-at-the-edge
property); a sliding-log or token-bucket avoids that at the cost of more state.
For coarse abuse protection on a control plane that is the right tradeoff.

Time is read from ``time.monotonic()`` so the limiter is immune to wall-clock
jumps; ``now`` is injectable for deterministic tests.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Window:
    start: float
    count: int


class FixedWindowRateLimiter:
    """Thread-safe fixed-window limiter.

    Sync FastAPI endpoints run in a threadpool, so the shared bucket map is
    guarded by a lock. Contention is negligible at this scale (the critical
    section is a dict lookup and an integer bump).
    """

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._buckets: dict[str, _Window] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Drop all counters. Used by tests and available for admin resets."""
        with self._lock:
            self._buckets.clear()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Record an attempt for ``key``; return ``(allowed, retry_after_s)``.

        ``retry_after_s`` is the whole seconds until the current window closes
        (``0`` when the request is allowed) — suitable for a ``Retry-After``
        header on a 429.
        """
        t = time.monotonic() if now is None else now
        with self._lock:
            w = self._buckets.get(key)
            if w is None or (t - w.start) >= self.window_s:
                # New key, or the previous window has fully elapsed: start fresh.
                self._buckets[key] = _Window(start=t, count=1)
                return True, 0
            if w.count < self.limit:
                w.count += 1
                return True, 0
            retry_after = int(self.window_s - (t - w.start)) + 1
            return False, retry_after
