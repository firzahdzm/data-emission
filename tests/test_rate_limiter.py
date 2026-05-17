import time

import pytest
from freezegun import freeze_time

from emission_tracker.rate_limiter import TokenBucket


def test_initial_capacity_allows_immediate_acquires():
    bucket = TokenBucket(capacity=5, refill_per_second=5 / 60)
    for _ in range(5):
        bucket.acquire()  # must not block


def test_acquire_blocks_when_empty(monkeypatch):
    """When bucket is empty, acquire sleeps until token refills."""
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    with freeze_time("2026-01-01 12:00:00") as frozen:
        bucket = TokenBucket(capacity=2, refill_per_second=1.0)
        bucket.acquire()
        bucket.acquire()  # bucket now empty

        bucket.acquire()  # must request sleep for ~1s
        assert len(sleeps) == 1
        assert 0.9 <= sleeps[0] <= 1.1


def test_refill_caps_at_capacity():
    with freeze_time("2026-01-01 12:00:00") as frozen:
        bucket = TokenBucket(capacity=3, refill_per_second=10.0)
        bucket.acquire()
        bucket.acquire()
        bucket.acquire()
        frozen.tick(60)  # 60 seconds elapse
        # bucket should be capped at 3, not 600
        for _ in range(3):
            bucket.acquire()  # must not block
