"""Wall-clock token bucket: the ±1 % pacing mechanism.

owed(t) = rate × (t − anchor); an event is released iff released < owed.
Catch-up is bounded: when the backlog exceeds rate × catchup_s the anchor
slides forward so at most catchup_s seconds of backlog is ever replayed,
and the discarded shortfall accumulates in discarded_s. lag_s is the
current backlog (against the current anchor) in seconds, capped by
construction at catchup_s.

The clock is injectable for deterministic tests; the default is wall time
because T0 is an absolute wall-clock instant shared across the fleet.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional


class TokenBucket(object):
    _WAIT_SLICE_S = 0.2  # bound on cond waits so pause/close are prompt

    def __init__(self, rate, catchup_s=5.0, clock=time.time):
        # type: (float, float, Callable[[], float]) -> None
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self._rate = float(rate)
        self._catchup_s = float(catchup_s)
        self._clock = clock
        self._cond = threading.Condition()
        self._anchor = clock()
        self._released = 0
        self._paused = False
        self._closed = False
        self._discarded_s = 0.0

    # -- lifecycle ---------------------------------------------------------

    def anchor_at(self, t0):
        # type: (float) -> None
        """Reset pacing to start at wall-clock t0 (release counter zeroed)."""
        with self._cond:
            self._anchor = float(t0)
            self._released = 0
            self._cond.notify_all()

    def pause(self):
        with self._cond:
            self._paused = True
            self._cond.notify_all()

    def resume(self):
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def close(self):
        """Permanently unblock waiters; acquire() returns False afterwards."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def retarget(self, new_rate):
        # type: (float) -> None
        """Change the rate in place, keeping owed(t) continuous at the switch
        so no burst or stall is introduced."""
        if new_rate <= 0:
            raise ValueError("rate must be > 0")
        with self._cond:
            now = self._clock()
            owed = self._rate * (now - self._anchor)
            self._anchor = now - owed / new_rate
            self._rate = float(new_rate)
            self._cond.notify_all()

    # -- accounting (call under lock) ---------------------------------------

    def _owed(self, now):
        return self._rate * (now - self._anchor)

    def _cap_backlog(self, now):
        max_backlog = self._rate * self._catchup_s
        backlog = self._owed(now) - self._released
        if backlog > max_backlog:
            self._discarded_s += (backlog - max_backlog) / self._rate
            self._anchor = now - (self._released + max_backlog) / self._rate

    # -- taking tokens -------------------------------------------------------

    def try_take(self):
        # type: () -> bool
        """Non-blocking: release one event if owed allows. False when paused,
        closed or ahead of quota."""
        with self._cond:
            if self._closed or self._paused:
                return False
            now = self._clock()
            self._cap_backlog(now)
            if self._released < self._owed(now):
                self._released += 1
                return True
            return False

    def acquire(self, timeout=None):
        # type: (Optional[float]) -> bool
        """Block until one event may be released. Returns False when closed
        or the timeout elapses; blocks indefinitely while paused."""
        deadline = None
        if timeout is not None:
            deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if self._closed:
                    return False
                if not self._paused:
                    now = self._clock()
                    self._cap_backlog(now)
                    if self._released < self._owed(now):
                        self._released += 1
                        return True
                    next_in = (self._released + 1 - self._owed(now)) / self._rate
                else:
                    next_in = self._WAIT_SLICE_S
                wait = min(max(next_in, 0.0), self._WAIT_SLICE_S)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait = min(wait, remaining)
                self._cond.wait(wait)

    # -- observability ---------------------------------------------------------

    def lag_s(self):
        # type: () -> float
        with self._cond:
            now = self._clock()
            self._cap_backlog(now)
            return max(0.0, self._owed(now) - self._released) / self._rate

    @property
    def rate(self):
        with self._cond:
            return self._rate

    @property
    def released(self):
        with self._cond:
            return self._released

    @property
    def discarded_s(self):
        """Cumulative seconds of quota discarded by bounded catch-up."""
        with self._cond:
            return self._discarded_s

    @property
    def paused(self):
        with self._cond:
            return self._paused

    @property
    def closed(self):
        with self._cond:
            return self._closed
