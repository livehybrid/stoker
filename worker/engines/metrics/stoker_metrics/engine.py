# -*- coding: utf-8 -*-
"""The Stoker metrics engine: synthetic Splunk metric data points.

A third worker engine alongside eventgen and Piston (rawreplay). Where eventgen
*templates* log events and Piston *replays* a recorded dataset, the metrics engine
*generates* Splunk metric measurements over time following a configured shape
(sine, business double-hump, ...). It emits one **multi-metric** HEC event per
dimension-combination per resolution tick:

    {"time": <tick epoch>, "host": null, "source": null, "sourcetype": null,
     "index": null, "event": "metric",
     "fields": {"metric_name:store.requests": 812, "metric_name:host.cpu.usage": 63.2,
                "product": "search", "region": "eu-west-1"}}

speaking the same AF_UNIX NDJSON protocol as the other engines (see
``docs/WORKER-CONTRACT.md``): connect to ``STOKER_OUTPUT_SOCKET`` and blocking
``sendall`` one envelope per line. The agent fills null metadata from the run
slice (so the metrics index/sourcetype come from the target/spec) and delivers to
HEC. The engine is **engine-paced** (count_interval / ungated): it emits its shard
of the series matrix on a wall-clock-aligned grid until the socket closes on drain.

Series matrix: the cross-product of the configured ``dimensions``; this worker owns
``series[slot::total_workers]`` (a deterministic stride so the fleet partitions the
matrix without overlap). Config is read from a JSON file (STOKER_METRICS_CONFIG)
the agent writes from the pack's ``metrics:`` block.

Dependency-light: stdlib only.
"""

from __future__ import absolute_import

import hashlib
import itertools
import json
import logging
import math
import os
import random
import select
import socket
import sys
import time

from . import patterns

log = logging.getLogger("stoker.metrics")

DEFAULT_SOCKET_PATH = "/tmp/stoker-output.sock"
DEFAULT_RESOLUTION_S = 10.0
_CONNECT_RETRY_S = 5.0
_CONNECT_RETRY_SLEEP_S = 0.02
# Idle poll cadence when this worker owns no series (workers > series).
_IDLE_POLL_S = 0.5


class MetricsError(Exception):
    """Fatal engine error (bad config, dead socket)."""


def _get(env, key):
    val = env.get(key)
    if val is None:
        return None
    val = val.strip()
    return val if val else None


class Config(object):
    def __init__(self, socket_path, spec, slot, total_workers, resolution_s):
        self.socket_path = socket_path
        self.spec = spec                    # the parsed metrics config dict
        self.slot = slot
        self.total_workers = total_workers
        self.resolution_s = resolution_s


def load_config(env=None):
    # type: (dict) -> Config
    if env is None:
        env = os.environ
    socket_path = _get(env, "STOKER_OUTPUT_SOCKET") or DEFAULT_SOCKET_PATH

    cfg_path = _get(env, "STOKER_METRICS_CONFIG")
    if not cfg_path:
        raise MetricsError("STOKER_METRICS_CONFIG is required and not set")
    if not os.path.isfile(cfg_path):
        raise MetricsError("STOKER_METRICS_CONFIG not found: %r" % cfg_path)
    try:
        with open(cfg_path, "r") as fh:
            spec = json.load(fh)
    except (OSError, ValueError) as exc:
        raise MetricsError("cannot read STOKER_METRICS_CONFIG %s: %s" % (cfg_path, exc))
    if not isinstance(spec, dict) or not spec.get("metrics"):
        raise MetricsError("metrics config has no `metrics` list")

    def _int(key, default, minimum):
        raw = _get(env, key)
        if raw is None:
            return default
        try:
            v = int(raw)
        except ValueError:
            raise MetricsError("%s must be an integer, got %r" % (key, raw))
        return max(minimum, v)

    slot = _int("STOKER_METRICS_SLOT", 0, 0)
    total = _int("STOKER_METRICS_TOTAL_WORKERS", 1, 1)
    if slot >= total:
        raise MetricsError("STOKER_METRICS_SLOT (%d) must be < TOTAL_WORKERS (%d)"
                           % (slot, total))

    res_raw = _get(env, "STOKER_METRICS_RESOLUTION_S")
    if res_raw is not None:
        try:
            resolution = float(res_raw)
        except ValueError:
            raise MetricsError("STOKER_METRICS_RESOLUTION_S must be a number")
    else:
        try:
            resolution = float(spec.get("resolution_s", DEFAULT_RESOLUTION_S))
        except (TypeError, ValueError):
            resolution = DEFAULT_RESOLUTION_S
    if resolution <= 0:
        raise MetricsError("resolution_s must be > 0, got %s" % resolution)

    return Config(socket_path, spec, slot, total, resolution)


def build_series(spec):
    # type: (dict) -> list
    """The full series matrix: the cross-product of the configured dimensions.

    Returns a deterministically ordered list of dicts ``{dim_key: value, ...}``.
    With no dimensions there is a single unlabelled series (``[{}]``).
    """
    dims = spec.get("dimensions") or []
    axes = []
    for d in dims:
        key = d.get("key")
        values = d.get("values") or []
        if key and values:
            axes.append((key, list(values)))
    if not axes:
        return [{}]
    series = []
    for combo in itertools.product(*[vals for _, vals in axes]):
        series.append({axes[i][0]: combo[i] for i in range(len(axes))})
    # Deterministic order (stable across workers so the stride shard is coherent).
    series.sort(key=lambda s: tuple(sorted(s.items())))
    return series


def _series_key(series):
    # type: (dict) -> str
    return "|".join("%s=%s" % (k, series[k]) for k in sorted(series))


def _stable_seed(*parts):
    # type: (...) -> int
    """A process-stable integer seed from string parts (hash() is salted)."""
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def _scale_for(metric, series):
    # type: (dict, dict) -> float
    """Product of the per-dimension-value multipliers for this metric+series."""
    scale = metric.get("scale") or {}
    mult = 1.0
    for dim_key, value in series.items():
        table = scale.get(dim_key)
        if isinstance(table, dict) and value in table:
            try:
                mult *= float(table[value])
            except (TypeError, ValueError):
                pass
    return mult


class MetricsEngine(object):
    """Generates the owned shard of the series matrix on a resolution grid."""

    def __init__(self, config, clock=time.time, sleep=time.sleep):
        # type: (Config, callable, callable) -> None
        self._cfg = config
        self._clock = clock
        self._sleep = sleep
        self.emitted = 0
        spec = config.spec
        all_series = build_series(spec)
        # This worker's deterministic stride shard of the matrix.
        self._series = all_series[config.slot::config.total_workers]
        self._metrics = spec.get("metrics") or []
        self._tz_offset_s = 3600.0 * float(spec.get("tz_offset_hours", 0) or 0)
        seed = spec.get("seed", 1234)
        # Per (series, metric) state: an independent RNG stream + walk/counter state.
        self._state = {}
        for si, series in enumerate(self._series):
            skey = _series_key(series)
            for metric in self._metrics:
                rng = random.Random(_stable_seed(seed, skey, metric.get("name")))
                self._state[(si, metric.get("name"))] = {"rng": rng}

    # -- value ------------------------------------------------------------- #

    def _value(self, si, metric, series, tick):
        # type: (int, dict, dict, float) -> float
        st = self._state[(si, metric.get("name"))]
        hour = ((tick + self._tz_offset_s) % 86400.0) / 3600.0
        a = patterns.activity(metric.get("pattern") or {}, hour, state=st)
        mult = _scale_for(metric, series)
        vmin = float(metric.get("min", 0.0)) * mult
        p95 = float(metric.get("p95", metric.get("max", 1.0))) * mult
        vmax = float(metric.get("max", p95)) * mult
        return patterns.sample_value(
            a, vmin, p95, vmax, metric.get("kind", "gauge"),
            float(metric.get("noise", 0.1) or 0.0), st["rng"], state=st)

    def _fields_for(self, si, series, tick):
        # type: (int, dict, float) -> dict
        fields = dict(series)  # dimensions become metric dimensions
        for metric in self._metrics:
            name = metric.get("name")
            if not name:
                continue
            fields["metric_name:" + name] = self._value(si, metric, series, tick)
        return fields

    # -- socket ------------------------------------------------------------ #

    def run(self):
        # type: () -> int
        cfg = self._cfg
        sock = _connect(cfg.socket_path, clock=self._clock, sleep=self._sleep)
        log.info("metrics connected to %s; series=%d metrics=%d resolution=%ss "
                 "slot=%d/%d", cfg.socket_path, len(self._series),
                 len(self._metrics), cfg.resolution_s, cfg.slot, cfg.total_workers)
        try:
            if not self._series:
                # This worker owns no series (workers > matrix size): idle until
                # the agent closes the socket on drain, then exit clean.
                self._idle_until_closed(sock)
                return 0
            self._run_grid(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass
        log.info("metrics finished; emitted %d measurements", self.emitted)
        return 0

    def _run_grid(self, sock):
        # type: (socket.socket) -> None
        res = self._cfg.resolution_s
        # Align the first tick to the wall-clock grid (…:00, :10, :20 for res=10).
        tick = math.floor(self._clock() / res) * res
        while True:
            now = self._clock()
            if now < tick:
                self._sleep(min(tick - now, 3600.0))
                continue
            for si, series in enumerate(self._series):
                envelope = _encode(tick, self._fields_for(si, series, tick))
                if not self._send(sock, envelope):
                    return  # socket closed on drain
            tick += res

    def _idle_until_closed(self, sock):
        # type: (socket.socket) -> None
        while True:
            try:
                r, _, _ = select.select([sock], [], [], _IDLE_POLL_S)
            except (OSError, ValueError):
                return
            if r:
                try:
                    if not sock.recv(1):
                        return  # peer closed
                except OSError:
                    return

    def _send(self, sock, data):
        # type: (socket.socket, bytes) -> bool
        try:
            sock.sendall(data)
        except (BrokenPipeError, ConnectionResetError):
            return False
        except OSError as exc:
            import errno
            if exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ESHUTDOWN):
                return False
            raise MetricsError("agent socket write failed: %s" % exc)
        self.emitted += 1
        return True


def _encode(time_value, fields):
    # type: (float, dict) -> bytes
    """One metric NDJSON envelope (UTF-8). ``event`` is the literal 'metric';
    metadata is null (the agent stamps index/sourcetype/host from the slice)."""
    try:
        tv = float(time_value)
        if not math.isfinite(tv):
            tv = None
    except (TypeError, ValueError):
        tv = None
    envelope = {
        "time": tv,
        "host": None,
        "source": None,
        "sourcetype": None,
        "index": None,
        "event": "metric",
        "fields": fields,
    }
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _connect(socket_path, deadline_s=_CONNECT_RETRY_S, clock=time.time,
             sleep=time.sleep):
    # type: (str, float, callable, callable) -> socket.socket
    deadline = clock() + deadline_s
    last_exc = None
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(socket_path)
            return sock
        except OSError as exc:
            last_exc = exc
            try:
                sock.close()
            except OSError:
                pass
            if clock() >= deadline:
                raise MetricsError("cannot connect to agent socket %s: %s"
                                   % (socket_path, last_exc))
            sleep(_CONNECT_RETRY_SLEEP_S)


def main(argv=None, env=None):
    # type: (list, dict) -> int
    level = (env or os.environ).get("STOKER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        cfg = load_config(env)
    except MetricsError as exc:
        sys.stderr.write("stoker-metrics: config error: %s\n" % exc)
        return 2
    try:
        return MetricsEngine(cfg).run()
    except MetricsError as exc:
        sys.stderr.write("stoker-metrics: %s\n" % exc)
        return 1
