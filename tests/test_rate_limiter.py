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


def test_two_threads_sharing_a_bucket_stay_within_the_rate():
    """The snapshot job and the balance job hit TaoStats from different
    threads against one shared bucket. If acquire() were not serialised the
    two would each see a full bucket and blow through the 5-per-minute cap.
    """
    import threading

    capacity, rate = 2, 10.0  # 10 tokens/sec keeps the test fast
    bucket = TokenBucket(capacity=capacity, refill_per_second=rate)

    per_thread = 5
    total = per_thread * 2
    stamps: list[float] = []
    stamps_lock = threading.Lock()

    def worker():
        for _ in range(per_thread):
            bucket.acquire()
            with stamps_lock:
                stamps.append(time.monotonic())

    started = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started

    assert len(stamps) == total
    # `capacity` acquires are free; the rest must wait for refill. Anything
    # faster means the two threads double-spent the same tokens.
    minimum = (total - capacity) / rate
    assert elapsed >= minimum * 0.95, (
        f"{total} acquires from 2 threads took {elapsed:.3f}s, "
        f"expected at least {minimum:.3f}s"
    )


def test_bucket_is_shared_across_every_taostats_caller():
    """One bucket instance must reach the snapshot job, the balance job and
    the admin-triggered runner — a second instance anywhere would silently
    double the effective rate against the same API key."""
    import inspect

    from emission_tracker import main

    source = inspect.getsource(main.create_app)
    assert source.count("TokenBucket(") == 1
