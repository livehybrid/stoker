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


class CpuTracker(object):
    """Process CPU percent between successive sample() calls, from
    /proc/self/stat utime+stime (fields 14 and 15, 1-based)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        self._last_cpu_s = self._read_cpu_s()
        self._last_wall = clock()

    def _read_cpu_s(self):
        # type: () -> float
        try:
            with open("/proc/self/stat", "r") as fh:
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
