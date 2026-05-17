import time
from threading import Lock


class TokenBucket:
    """Thread-safe token bucket rate limiter."""

    def __init__(self, capacity: int, refill_per_second: float):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        self._capacity = capacity
        self._refill_rate = refill_per_second
        self._tokens = float(capacity)
        self._last_refill = time.time()
        self._lock = Lock()

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._refill_rate,
        )
        self._last_refill = now

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            missing = 1.0 - self._tokens
            wait = missing / self._refill_rate
            # After sleeping `wait` seconds, exactly one token will have refilled.
            # Consume it now; bookkeeping will advance _last_refill so future
            # refills account for the sleep we just performed.
            self._tokens = 0.0
            self._last_refill += wait
        time.sleep(wait)
