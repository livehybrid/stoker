"""Prometheus metrics and /proc/self resource sampling (no psutil).

STOKER_METRICS_PORT = 0 disables the exporter entirely; prometheus_client
is only imported when the exporter starts, so disabled runs (and unit
tests) carry no dependency on it.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

log = logging.getLogger("stoker.metrics")

_PAGE_NAMES = (
    "events_total", "bytes_total", "hec_2xx", "hec_4xx", "hec_5xx",
    "hec_timeouts", "retries", "dropped", "queue_depth",
)


def read_rss_mb():
    # type: () -> float
    """Resident set size in MB from /proc/self/status (VmRSS is in kB)."""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def read_cpu_limit_cores():
    # type: () -> Optional[float]
    """Cores this container may use: the cgroup CPU quota when one is set
    (v2 cpu.max, else v1 cfs quota/period), otherwise the CPUs the process
    is allowed to run on. Lets an engine CPU figure be read against its
    ceiling: 100 % engine CPU is saturated on a 1-core limit and idle-ish on
    an 8-core node."""
    try:
        with open("/sys/fs/cgroup/cpu.max", "r") as fh:
            quota, period = fh.read().split()[:2]
        if quota != "max" and int(period) > 0:
            return round(int(quota) / float(period), 2)
    except (OSError, ValueError):
        try:
            with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r") as fh:
                quota_us = int(fh.read().strip())
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "r") as fh:
                period_us = int(fh.read().strip())
            if quota_us > 0 and period_us > 0:
                return round(quota_us / float(period_us), 2)
        except (OSError, ValueError):
            pass
    try:
        return float(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        count = os.cpu_count()
        return float(count) if count else None


class CpuTracker(object):
    """Process CPU percent between successive sample() calls, from
    /proc/<pid>/stat utime+stime (fields 14 and 15, 1-based). The figure
    counts every thread of the process, so 100 is one core and a busy
    multi-threaded engine reads above it."""

    def __init__(self, clock=time.monotonic, stat_path="/proc/self/stat"):
        self._clock = clock
        self._stat_path = stat_path
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._last_cpu_s = self._read_cpu_s()
        self._last_wall = clock()

    def _read_cpu_s(self):
        # type: () -> float
        try:
            with open(self._stat_path, "r") as fh:
                data = fh.read()
            # comm may contain spaces; fields are counted after the final ')'
            fields = data.rsplit(")", 1)[1].split()
            utime, stime = int(fields[11]), int(fields[12])
            return (utime + stime) / float(self._hz)
        except (OSError, ValueError, IndexError):
            return 0.0

    def sample(self):
        # type: () -> float
        now = self._clock()
        cpu_s = self._read_cpu_s()
        wall_delta = now - self._last_wall
        pct = 0.0
        if wall_delta > 0:
            pct = 100.0 * (cpu_s - self._last_cpu_s) / wall_delta
        self._last_cpu_s = cpu_s
        self._last_wall = now
        return max(0.0, pct)


class EngineCpuTracker(object):
    """CPU percent of the engine subprocess, which the agent can restart
    under a new pid. The agent's own CpuTracker never sees it: firebox does
    the generating in its own process, so a worker whose agent reads 5 %
    can have an engine pinned at its core allowance."""

    def __init__(self, clock=time.monotonic, proc_root="/proc"):
        self._clock = clock
        self._proc_root = proc_root
        self._pid = None  # type: Optional[int]
        self._tracker = None  # type: Optional[CpuTracker]

    def sample(self, pid):
        # type: (Optional[int]) -> Optional[float]
        """Percent since the last call for the same pid; None while there is
        no engine, and on the first call after it (re)starts."""
        if not isinstance(pid, int) or pid <= 0:
            self._pid, self._tracker = None, None
            return None
        if pid != self._pid:
            self._pid = pid
            self._tracker = CpuTracker(
                self._clock, os.path.join(self._proc_root, str(pid), "stat"))
            return None
        return self._tracker.sample()


class Metrics(object):
    """Gauge exporter; values are pushed from the heartbeat loop."""

    def __init__(self, port):
        # type: (int) -> None
        self._port = int(port)
        self._gauges = {}  # type: Dict[str, Any]
        self._started = False

    def start(self):
        if self._port <= 0:
            return
        from prometheus_client import Gauge, start_http_server
        for name in _PAGE_NAMES:
            self._gauges[name] = Gauge("stoker_" + name,
                                       "Stoker agent counter " + name)
        for name, help_text in (
            ("eps", "Measured events per second over the last interval"),
            ("lag_s", "Pacing backlog seconds against the current anchor"),
            ("rss_mb", "Agent resident set size in MB"),
            ("cpu_pct", "Agent CPU percent over the last interval"),
        ):
            self._gauges[name] = Gauge("stoker_" + name, help_text)
        start_http_server(self._port)
        self._started = True
        log.info("metrics exporter listening on :%d", self._port)

    def update(self, snapshot, eps=0.0, lag_s=0.0, rss_mb=0.0, cpu_pct=0.0):
        # type: (Dict[str, Any], float, float, float, float) -> None
        if not self._started:
            return
        for name in _PAGE_NAMES:
            if name in snapshot:
                self._gauges[name].set(snapshot[name])
        self._gauges["eps"].set(eps)
        self._gauges["lag_s"].set(lag_s)
        self._gauges["rss_mb"].set(rss_mb)
        self._gauges["cpu_pct"].set(cpu_pct)
