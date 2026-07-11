"""Agent orchestration: claim, prepare, pace, heartbeat, drain.

The Agent wires config -> control -> bundle -> conf rewrite -> socket
listener -> engine -> run loop, and owns the drain path. Collaborators are
injectable (hec_factory, engine_factory, control) so the whole flow is
testable without eventgen or a live control plane.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Optional

from . import bundle as bundle_mod
from . import confrewrite
from .config import Config
from .control import (ControlClient, DeadManError, StandaloneControl,
                      SupersededError)
from .engine import STOP_GRACE_S, EngineError, EngineRunner
from .metrics import CpuTracker, Metrics, read_rss_mb
from .pacing import TokenBucket
from .slice import SliceError, SpecSlice, parse_iso8601
from .sockserver import SocketServer, make_filler

log = logging.getLogger("stoker.agent")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_AUTH_STANDALONE = 3
EXIT_DEADMAN = 4

RETARGET_IN_PLACE_BAND = 0.15
DEFAULT_BYTES_PER_EVENT = 256.0
FLUSH_TIMEOUT_S = 20.0

_HEARTBEAT_KEYS = ("events_total", "bytes_total", "hec_2xx", "hec_4xx",
                   "hec_5xx", "hec_timeouts", "retries", "queue_depth")


def _default_hec_factory(url, token, gzip_enabled, verify_tls, ack):
    # Imported lazily: hec_client needs requests wiring only at runtime.
    from .hec_client import HecClient
    return HecClient(url, token, gzip_enabled=gzip_enabled,
                     verify_tls=verify_tls, ack=ack)


def _default_engine_factory(conf_path, socket_path, cwd=None):
    return EngineRunner(conf_path, socket_path, cwd=cwd)


class Agent(object):
    def __init__(self, config, hec_factory=None, engine_factory=None,
                 control=None, clock=time.time):
        # type: (Config, Optional[Callable], Optional[Callable], Optional[Any], Callable[[], float]) -> None
        self._cfg = config
        self._hec_factory = hec_factory or _default_hec_factory
        self._engine_factory = engine_factory or _default_engine_factory
        self._control_override = control
        self._clock = clock
        self._drain_event = threading.Event()
        self._drain_reason = None  # type: Optional[str]
        self._exit_code = EXIT_OK
        self._state = "starting"
        self._fencing_paused = False
        # resources, torn down in run()'s finally
        self._hec = None
        self._bucket = None
        self._sock = None
        self._engine = None
        self._engine_started = False
        # measured-eps window
        self._last_events = 0
        self._last_events_t = None  # type: Optional[float]

    # -- public ------------------------------------------------------------

    def request_drain(self, reason):
        # type: (str) -> None
        if not self._drain_event.is_set():
            log.info("drain requested: %s", reason)
            self._drain_reason = reason
            self._drain_event.set()

    @property
    def state(self):
        return self._state

    def run(self):
        # type: () -> int
        cfg = self._cfg
        workdir = tempfile.mkdtemp(prefix="stoker-run-")
        control = None
        sl = None
        cpu = CpuTracker()
        metrics = Metrics(cfg.metrics_port)
        conf_path = os.path.join(workdir, "eventgen.conf")
        try:
            try:
                control, sl = self._claim(cfg)
                self._state = "preparing"
                pack = bundle_mod.fetch_bundle(
                    sl.bundle_url, workdir,
                    sha256=sl.bundle_sha256, jwt=cfg.run_jwt)
                gated = sl.rate_mode != "count_interval"
                share_eps = self._gating_eps(sl, pack.estimates)
                confrewrite.rewrite_file(
                    pack.conf_path, conf_path, sl.rate_mode, sl.rate_value,
                    cfg.overdrive, pack.samples_dir,
                    slot=sl.slot, total_workers=sl.total_workers)

                self._hec = self._hec_factory(
                    sl.hec_url, cfg.hec_token, gzip_enabled=sl.hec_gzip,
                    verify_tls=cfg.hec_verify_tls, ack=sl.hec_ack)
                self._bucket = TokenBucket(share_eps or 1.0, cfg.catchup_s,
                                           self._clock)
                self._bucket.pause()  # nothing flows before T0
                # park the anchor far ahead so pre-release lag_s reads 0
                self._bucket.anchor_at(self._clock() + 1e9)
                metrics.start()
                self._sock = SocketServer(cfg.output_socket, self._hec,
                                          self._bucket, make_filler(sl),
                                          gated=gated)
                self._sock.start()
                # cwd rooted at the pack so eventgen resolves relative
                # file-token replacement paths (e.g. samples/foo.sample)
                # against the pack, not the container working directory.
                self._engine = self._engine_factory(conf_path,
                                                    cfg.output_socket,
                                                    pack.pack_dir)
                if gated:
                    # warm the engine; the paused bucket holds output back
                    self._engine.start()
                    self._engine_started = True

                self._state = "ready"
                control.ready(sl.slot, sl.lease_id)
                t0 = self._await_release(control, sl)
                if t0 is not None:
                    anchor = sl.effective_t0 if sl.effective_t0 else t0
                    self._wait_until(t0)
                    if not self._drain_event.is_set():
                        self._bucket.anchor_at(anchor)
                        self._bucket.resume()
                        if not gated and not self._engine_started:
                            # count_interval is engine-paced: start at T0
                            self._engine.start()
                            self._engine_started = True
                        self._state = "generating"
                        deadline = (anchor + sl.duration_s) \
                            if sl.duration_s else None
                        self._run_loop(control, sl, deadline, cpu, metrics,
                                       conf_path, pack)
            except DeadManError as exc:
                log.error("dead-man: %s", exc)
                self._exit_code = EXIT_DEADMAN
                self.request_drain("dead-man")
            except SupersededError as exc:
                log.error("superseded: %s", exc)
                self.request_drain("superseded")
            except (bundle_mod.BundleError, SliceError,
                    confrewrite.ConfRewriteError, EngineError) as exc:
                log.error("setup failed: %s", exc)
                self._exit_code = EXIT_CONFIG
                self.request_drain("setup-failure")

            self._shutdown(control, sl)
            return self._exit_code
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- phases -----------------------------------------------------------

    def _claim(self, cfg):
        # type: (Config) -> tuple
        if cfg.standalone:
            control = self._control_override or StandaloneControl(self._clock)
            return control, SpecSlice.from_standalone(cfg)
        control = self._control_override or ControlClient(
            cfg.control_url, cfg.run_id, cfg.run_jwt, deadman_s=cfg.deadman_s)
        self._state = "claiming"
        doc = control.claim(cfg.holder, cfg.hint_slot)
        return control, SpecSlice.from_claim(doc)

    def _gating_eps(self, sl, estimates):
        # type: (SpecSlice, Dict[str, Any]) -> Optional[float]
        if sl.rate_mode == "eps":
            return float(sl.rate_value)
        if sl.rate_mode == "per_day_gb":
            bpe = estimates.get("bytes_per_event")
            try:
                bpe = float(bpe) if bpe else 0.0
            except (TypeError, ValueError):
                bpe = 0.0
            if bpe <= 0:
                bpe = DEFAULT_BYTES_PER_EVENT
                log.warning("bundle declares no bytes_per_event estimate; "
                            "gating per_day_gb with %.0f B/event", bpe)
            return sl.rate_value * 1e9 / bpe / 86400.0
        return None  # count_interval: engine-paced, no gating

    def _await_release(self, control, sl):
        # type: (Any, SpecSlice) -> Optional[float]
        """Poll heartbeat until the release command; returns T0 (epoch)."""
        if sl.released:
            # claim already released (re-issued lease): effective_t0 anchors
            return sl.effective_t0 if sl.effective_t0 else self._clock()
        while not self._drain_event.is_set():
            self._check_fencing(control)
            resp = control.heartbeat(self._heartbeat_payload(sl))
            if resp is not None:
                command = resp.get("command")
                if command == "release":
                    return parse_iso8601(resp["t0"]) if resp.get("t0") \
                        else self._clock()
                if command == "drain":
                    self.request_drain("control-drain")
                    return None
            # Dead-man also applies before T0: heartbeat() swallows transport
            # failures to None, so a control plane that dies after ready() but
            # before releasing us would otherwise hang here forever (only
            # SIGTERM would break it). Self-evict after the dead-man window so
            # the fleet slot is released. StandaloneControl never expires.
            if control.deadman_expired():
                self._exit_code = EXIT_DEADMAN
                self.request_drain("dead-man")
                return None
            self._drain_event.wait(sl.telemetry_interval_s)
        return None

    def _wait_until(self, t0):
        # type: (float) -> None
        while not self._drain_event.is_set():
            remaining = t0 - self._clock()
            if remaining <= 0:
                return
            self._drain_event.wait(min(remaining, 0.05))

    def _run_loop(self, control, sl, deadline, cpu, metrics, conf_path, pack):
        # type: (Any, SpecSlice, Optional[float], CpuTracker, Metrics, str, Any) -> None
        interval = sl.telemetry_interval_s
        next_hb = time.monotonic() + interval
        while not self._drain_event.is_set():
            now_wall = self._clock()
            if deadline is not None and now_wall >= deadline:
                self.request_drain("duration-complete")
                break
            if self._engine_started and not self._engine.is_alive():
                log.warning("engine exited (rc=%s); draining",
                            self._engine.returncode)
                self.request_drain("engine-exit")
                break
            snap = self._hec.snapshot()
            if snap.get("auth_failed"):
                if self._cfg.standalone:
                    self._exit_code = EXIT_AUTH_STANDALONE
                self.request_drain("hec-auth-failed")
                break
            if control.deadman_expired():
                self._exit_code = EXIT_DEADMAN
                self.request_drain("dead-man")
                break
            self._check_fencing(control)

            if time.monotonic() >= next_hb:
                next_hb = time.monotonic() + interval
                payload = self._heartbeat_payload(sl, cpu=cpu, snap=snap)
                metrics.update(snap, eps=payload["eps"],
                               lag_s=payload["lag_s"],
                               rss_mb=payload["rss_mb"],
                               cpu_pct=payload["cpu_pct"])
                resp = control.heartbeat(payload)  # may raise Superseded
                if resp is not None:
                    self._check_fencing(control)  # ack may lift the pause
                    self._apply_command(resp, sl, conf_path, pack)

            timeout = max(0.0, next_hb - time.monotonic())
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - self._clock()))
            self._drain_event.wait(min(timeout, interval) or 0.001)

    def _check_fencing(self, control):
        # type: (Any) -> None
        if self._cfg.standalone or self._bucket is None:
            return
        if control.should_pause():
            if not self._fencing_paused:
                log.warning("fencing: %.0f s without heartbeat ack; pausing",
                            control.seconds_since_ack())
                self._bucket.pause()
                self._fencing_paused = True
                self._state = "paused"
        elif self._fencing_paused:
            log.info("fencing: heartbeat ack confirmed lease; resuming")
            self._bucket.resume()
            self._fencing_paused = False
            self._state = "generating"

    def _apply_command(self, resp, sl, conf_path, pack):
        # type: (Dict[str, Any], SpecSlice, str, Any) -> None
        command = resp.get("command")
        if command in (None, "continue", "release"):
            return
        if command == "drain":
            self.request_drain("control-drain")
        elif command == "retarget":
            self._retarget(resp.get("share") or {}, sl, conf_path, pack)
        else:
            log.warning("unknown control command %r ignored", command)

    def _retarget(self, share, sl, conf_path, pack):
        # type: (Dict[str, Any], SpecSlice, str, Any) -> None
        if len(share) != 1:
            log.warning("retarget share must carry exactly one key: %r", share)
            return
        key = next(iter(share))
        mode = {"eps": "eps", "per_day_gb": "per_day_gb",
                "count": "count_interval"}.get(key)
        if mode != sl.rate_mode:
            log.warning("retarget mode %r does not match run mode %r; ignored",
                        key, sl.rate_mode)
            return
        try:
            new_value = float(share[key])
        except (TypeError, ValueError):
            log.warning("retarget share.%s not numeric: %r", key, share[key])
            return
        if new_value <= 0:
            log.warning("retarget share.%s must be > 0", key)
            return
        old_value = sl.rate_value
        sl.rate_value = new_value
        new_eps = self._gating_eps(sl, pack.estimates)
        if sl.rate_mode == "count_interval":
            log.info("retarget count %s -> %s: conf rewrite + engine restart",
                     old_value, new_value)
            self._rewrite_and_restart(sl, conf_path, pack)
            return
        ratio = new_value / old_value if old_value else float("inf")
        if abs(ratio - 1.0) <= RETARGET_IN_PLACE_BAND:
            log.info("retarget %s %.6g -> %.6g in place", sl.rate_mode,
                     old_value, new_value)
            self._bucket.retarget(new_eps)
        else:
            log.info("retarget %s %.6g -> %.6g beyond headroom: conf rewrite "
                     "+ engine restart (expect a 5-10 s gap)", sl.rate_mode,
                     old_value, new_value)
            self._bucket.retarget(new_eps)
            self._rewrite_and_restart(sl, conf_path, pack)

    def _rewrite_and_restart(self, sl, conf_path, pack):
        # type: (SpecSlice, str, Any) -> None
        confrewrite.rewrite_file(
            pack.conf_path, conf_path, sl.rate_mode, sl.rate_value,
            self._cfg.overdrive, pack.samples_dir,
            slot=sl.slot, total_workers=sl.total_workers)
        gap_start = time.monotonic()
        self._engine.restart()
        self._engine_started = True
        log.info("engine restarted in %.1f s", time.monotonic() - gap_start)

    # -- telemetry ---------------------------------------------------------

    def _measured_eps(self, events_total):
        # type: (int) -> float
        now = time.monotonic()
        if self._last_events_t is None:
            self._last_events, self._last_events_t = events_total, now
            return 0.0
        delta_t = now - self._last_events_t
        eps = (events_total - self._last_events) / delta_t if delta_t > 0 \
            else 0.0
        self._last_events, self._last_events_t = events_total, now
        return round(eps, 3)

    def _heartbeat_payload(self, sl, cpu=None, snap=None):
        # type: (SpecSlice, Optional[CpuTracker], Optional[Dict[str, Any]]) -> Dict[str, Any]
        if snap is None:
            snap = self._hec.snapshot() if self._hec else {}
        payload = {"slot": sl.slot, "lease_id": sl.lease_id}
        for key in _HEARTBEAT_KEYS:
            payload[key] = snap.get(key, 0)
        payload["dropped"] = snap.get("dropped", 0)
        payload["eps"] = self._measured_eps(payload["events_total"])
        payload["lag_s"] = round(self._bucket.lag_s(), 3) if self._bucket \
            and not self._bucket.closed else 0.0
        payload["rss_mb"] = round(read_rss_mb(), 1)
        payload["cpu_pct"] = round(cpu.sample(), 1) if cpu else 0.0
        payload["state"] = self._state
        if snap.get("auth_failed"):
            payload["auth_failed"] = True
        return payload

    # -- drain ---------------------------------------------------------------

    def _shutdown(self, control, sl):
        # type: (Any, Optional[SpecSlice]) -> None
        self._state = "draining"
        reason = self._drain_reason or "complete"
        log.info("draining (%s)", reason)
        # Every stage below is clamped against one global deadline so the whole
        # drain stays within the SIGTERM budget even when both the HEC and the
        # control plane are unreachable (their per-stage timeouts would
        # otherwise sum well past it).
        drain_deadline = time.monotonic() + self._cfg.drain_budget_s

        def remaining():
            # type: () -> float
            return max(0.0, drain_deadline - time.monotonic())

        # Stop intake first so pacing stays exact: unreleased socket data is
        # dropped by design; only the HEC queue is flushed.
        if self._bucket is not None:
            self._bucket.close()
        # Signal the HEC client to stop before joining the socket reader: a
        # reader parked inside hec.put() on a full queue (degraded HEC) is only
        # released by this, not by bucket.close(), so without it the socket
        # join would burn its whole timeout for nothing.
        if self._hec is not None:
            self._hec.begin_stop()
        if self._sock is not None:
            self._sock.stop(join_timeout_s=min(5.0, remaining()))
        if self._engine is not None and self._engine_started:
            self._engine.stop(grace_s=min(STOP_GRACE_S, remaining()))
        flushed = True
        summary = {}  # type: Dict[str, Any]
        if self._hec is not None:
            flushed = self._hec.flush_and_stop(min(FLUSH_TIMEOUT_S, remaining()))
            summary = self._hec.snapshot()
            if self._cfg.standalone and summary.get("auth_failed"):
                self._exit_code = EXIT_AUTH_STANDALONE
        summary["reason"] = reason
        summary["flushed"] = flushed
        summary["state"] = "drained"
        if self._sock is not None:
            summary["socket_received"] = self._sock.received
            summary["socket_malformed"] = self._sock.malformed
        if self._bucket is not None:
            summary["discarded_s"] = round(self._bucket.discarded_s, 3)
        log_tail = self._engine.log_tail() if self._engine is not None else []
        if control is not None and sl is not None:
            control.final(sl.slot, summary, log_tail, deadline=drain_deadline)
        log.info("drain complete: %s", summary)
