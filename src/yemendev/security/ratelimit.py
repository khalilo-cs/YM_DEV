"""In-process token-bucket rate limiting (audit finding A-14).

* One bucket per key (a credential hash, or a client address for failed authentication).
* Memory is bounded: at most ``max_keys`` buckets, least-recently-used evicted first.
* Thread-safe. Single-process only: limits are not shared between server processes.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class TokenBucketLimiter:
    def __init__(self, rate_per_minute: float, burst: int, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if rate_per_minute <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self.rate = rate_per_minute / 60.0
        self.burst = float(burst)
        self.max_keys = max_keys
        self.clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def _refill(self, key: str, now: float) -> float:
        tokens, last = self._buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        return tokens

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Consume ``cost`` tokens. Returns (allowed, seconds_until_allowed)."""
        with self._lock:
            now = self.clock()
            tokens = self._refill(key, now)
            if tokens >= cost:
                self._store(key, tokens - cost, now)
                return True, 0.0
            self._store(key, tokens, now)
            return False, (cost - tokens) / self.rate

    def peek(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """Check without consuming."""
        with self._lock:
            tokens = self._refill(key, self.clock())
            return (True, 0.0) if tokens >= cost else (False, (cost - tokens) / self.rate)

    def _store(self, key: str, tokens: float, now: float) -> None:
        self._buckets[key] = (tokens, now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)

    def __len__(self) -> int:
        return len(self._buckets)
