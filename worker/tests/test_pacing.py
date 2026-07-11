import threading
import time

import pytest

from stoker_agent.pacing import TokenBucket


class FakeClock(object):
    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make(rate=10.0, catchup_s=5.0, start=1000.0):
    clock = FakeClock(start)
    bucket = TokenBucket(rate, catchup_s=catchup_s, clock=clock)
    bucket.anchor_at(start)
    return bucket, clock


def drain_available(bucket):
    n = 0
    while bucket.try_take():
        n += 1
    return n


class TestOwedAccrual:
    def test_nothing_owed_at_anchor(self):
        bucket, _ = make(rate=10)
        assert bucket.try_take() is False

    def test_owed_accrues_with_clock(self):
        bucket, clock = make(rate=10)
        clock.advance(1.0)  # owed = 10
        assert drain_available(bucket) == 10
        assert bucket.try_take() is False

    def test_fractional_rate(self):
        # contract: release iff released < owed, so the first event goes as
        # soon as any quota at all has accrued
        bucket, clock = make(rate=0.5)
        clock.advance(0.5)  # owed = 0.25
        assert bucket.try_take() is True
        assert bucket.try_take() is False
        clock.advance(2.0)  # owed = 1.25, released = 1
        assert bucket.try_take() is True
        assert bucket.try_take() is False

    def test_future_anchor_releases_nothing(self):
        bucket, clock = make(rate=100)
        bucket.anchor_at(clock.t + 60)
        clock.advance(10)
        assert bucket.try_take() is False
        assert bucket.lag_s() == 0.0

    def test_released_counter(self):
        bucket, clock = make(rate=10)
        clock.advance(0.5)
        assert drain_available(bucket) == 5
        assert bucket.released == 5


class TestBoundedCatchup:
    def test_backlog_capped_at_catchup_seconds(self):
        bucket, clock = make(rate=10, catchup_s=5)
        clock.advance(100.0)  # owed 1000, backlog must cap at 50
        assert bucket.lag_s() == pytest.approx(5.0)
        assert drain_available(bucket) == 50

    def test_discarded_shortfall_exposed(self):
        bucket, clock = make(rate=10, catchup_s=5)
        clock.advance(100.0)
        bucket.try_take()  # forces the anchor slide
        assert bucket.discarded_s == pytest.approx(95.0)

    def test_no_discard_within_bound(self):
        bucket, clock = make(rate=10, catchup_s=5)
        clock.advance(4.0)
        drain_available(bucket)
        assert bucket.discarded_s == 0.0

    def test_lag_reflects_partial_backlog(self):
        bucket, clock = make(rate=10, catchup_s=5)
        clock.advance(2.0)  # owed 20, nothing released
        assert bucket.lag_s() == pytest.approx(2.0)


class TestRetarget:
    def test_owed_continuous_at_switch(self):
        bucket, clock = make(rate=10)
        clock.advance(2.0)  # owed 20
        bucket.retarget(20.0)
        assert drain_available(bucket) == 20  # unchanged at the switch
        clock.advance(1.0)  # +20 at the new rate
        assert drain_available(bucket) == 20

    def test_retarget_down(self):
        bucket, clock = make(rate=100)
        clock.advance(1.0)
        assert drain_available(bucket) == 100
        bucket.retarget(10.0)
        clock.advance(1.0)
        assert drain_available(bucket) == 10

    def test_rate_property(self):
        bucket, _ = make(rate=10)
        bucket.retarget(11.5)
        assert bucket.rate == 11.5

    def test_invalid_rate_rejected(self):
        bucket, _ = make()
        with pytest.raises(ValueError):
            bucket.retarget(0)
        with pytest.raises(ValueError):
            TokenBucket(0)


class TestPauseResume:
    def test_paused_releases_nothing(self):
        bucket, clock = make(rate=10)
        clock.advance(1.0)
        bucket.pause()
        assert bucket.try_take() is False
        assert bucket.paused is True

    def test_resume_releases_capped_backlog(self):
        bucket, clock = make(rate=10, catchup_s=5)
        bucket.pause()
        clock.advance(60.0)
        bucket.resume()
        assert drain_available(bucket) == 50  # catch-up bound applies

    def test_acquire_blocks_while_paused(self):
        bucket = TokenBucket(1000.0, catchup_s=5)
        bucket.anchor_at(time.time() - 1)
        bucket.pause()
        result = []
        t = threading.Thread(target=lambda: result.append(bucket.acquire()))
        t.start()
        time.sleep(0.15)
        assert result == []  # still blocked
        bucket.resume()
        t.join(2.0)
        assert result == [True]


class TestClose:
    def test_acquire_returns_false_after_close(self):
        bucket, _ = make()
        bucket.close()
        assert bucket.acquire() is False
        assert bucket.try_take() is False
        assert bucket.closed is True

    def test_close_unblocks_waiter(self):
        bucket = TokenBucket(100.0, catchup_s=5)
        bucket.anchor_at(time.time() + 3600)  # future anchor: nothing owed
        result = []
        t = threading.Thread(target=lambda: result.append(bucket.acquire()))
        t.start()
        time.sleep(0.05)
        bucket.close()
        t.join(2.0)
        assert result == [False]


class TestBlockingAcquire:
    def test_acquire_timeout(self):
        bucket = TokenBucket(100.0, catchup_s=5)
        bucket.anchor_at(time.time() + 3600)  # future anchor: nothing owed
        start = time.monotonic()
        assert bucket.acquire(timeout=0.1) is False
        assert time.monotonic() - start < 1.0

    def test_acquire_paces_real_time(self):
        bucket = TokenBucket(100.0, catchup_s=5)
        bucket.anchor_at(time.time())
        start = time.monotonic()
        for _ in range(20):
            assert bucket.acquire(timeout=2.0) is True
        elapsed = time.monotonic() - start
        # 20 events at 100 eps is 0.2 s of quota
        assert 0.1 <= elapsed <= 1.0
