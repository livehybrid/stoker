"""Eventgen subprocess management.

Builds the engine command (STOKER_ENGINE_CMD override, otherwise the
contract fallback `python -m splunk_eventgen generate <conf>` with the
vendored tree on PYTHONPATH), captures the last 50 output lines in a ring
buffer for the final POST and stops with SIGTERM, 10 s grace, SIGKILL.
"""

from __future__ import annotations

import collections
import logging
import os
import shlex
import subprocess
import sys
import threading
from typing import Dict, List, Optional

log = logging.getLogger("stoker.engine")

DEFAULT_RING_SIZE = 50
STOP_GRACE_S = 10.0

_HERE = os.path.dirname(os.path.abspath(__file__))
# worker/stoker_agent/ -> worker/engines/eventgen
DEFAULT_ENGINE_ROOT = os.path.join(os.path.dirname(_HERE), "engines", "eventgen")
# worker/stoker_agent/ -> worker/engines/rawreplay (the PISTON engine package root)
DEFAULT_RAWREPLAY_ROOT = os.path.join(os.path.dirname(_HERE), "engines", "rawreplay")
# worker/stoker_agent/ -> worker/engines/metrics (the metrics engine package root)
DEFAULT_METRICS_ROOT = os.path.join(os.path.dirname(_HERE), "engines", "metrics")


class EngineError(Exception):
    pass


def build_command(conf_path, env=None):
    # type: (str, Optional[Dict[str, str]]) -> List[str]
    """Engine invocation. STOKER_ENGINE_CMD (shell-quoted, `{conf}`
    placeholder or conf appended) lets ENGINE-NOTES supply a different
    launcher without a code change."""
    env = env if env is not None else os.environ
    override = env.get("STOKER_ENGINE_CMD")
    if override:
        parts = shlex.split(override)
        if "{conf}" in parts:
            return [conf_path if p == "{conf}" else p for p in parts]
        return parts + [conf_path]
    return [sys.executable, "-m", "splunk_eventgen", "generate", conf_path]


def build_rawreplay_command(env=None):
    # type: (Optional[Dict[str, str]]) -> List[str]
    """PISTON invocation: ``python -m stoker_rawreplay``.

    Takes no conf argument (the rawreplay engine reads its whole configuration
    from the environment: STOKER_OUTPUT_SOCKET + the STOKER_RAWREPLAY_* vars).
    STOKER_RAWREPLAY_CMD (shell-quoted) lets ENGINE-NOTES supply an alternate
    launcher without a code change, mirroring STOKER_ENGINE_CMD for eventgen."""
    env = env if env is not None else os.environ
    override = env.get("STOKER_RAWREPLAY_CMD")
    if override:
        return shlex.split(override)
    return [sys.executable, "-m", "stoker_rawreplay"]


def build_metrics_command(env=None):
    # type: (Optional[Dict[str, str]]) -> List[str]
    """Metrics-engine invocation: ``python -m stoker_metrics``.

    Reads its whole configuration from the environment (STOKER_OUTPUT_SOCKET +
    the STOKER_METRICS_* vars). STOKER_METRICS_CMD (shell-quoted) overrides the
    launcher, mirroring STOKER_ENGINE_CMD / STOKER_RAWREPLAY_CMD."""
    env = env if env is not None else os.environ
    override = env.get("STOKER_METRICS_CMD")
    if override:
        return shlex.split(override)
    return [sys.executable, "-m", "stoker_metrics"]


class EngineRunner(object):
    def __init__(self, conf_path, socket_path, engine_root=None,
                 extra_env=None, ring_size=DEFAULT_RING_SIZE, cwd=None):
        # type: (str, str, Optional[str], Optional[Dict[str, str]], int, Optional[str]) -> None
        self._conf_path = conf_path
        self._socket_path = socket_path
        self._engine_root = engine_root or DEFAULT_ENGINE_ROOT
        self._extra_env = dict(extra_env or {})
        # Working directory for the engine subprocess. eventgen resolves
        # relative file-token replacement paths against it; rooting it at the
        # pack makes `samples/foo.sample` resolve correctly. None inherits the
        # agent's cwd (the pre-fix behaviour).
        self._cwd = cwd
        self._ring = collections.deque(maxlen=ring_size)
        self._ring_lock = threading.Lock()
        self._proc = None    # type: Optional[subprocess.Popen]
        self._reader = None  # type: Optional[threading.Thread]

    def _build_env(self):
        # type: () -> Dict[str, str]
        env = dict(os.environ)
        env.update(self._extra_env)
        env["STOKER_OUTPUT_SOCKET"] = self._socket_path
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = self._engine_root + (
            os.pathsep + existing if existing else "")
        # eventgen opens rotating log files at import time (ENGINE-NOTES);
        # the directory must exist and be writable or the engine dies
        if not env.get("EVENTGEN_LOG_DIR"):
            log_dir = os.path.join(os.path.dirname(self._conf_path)
                                   or ".", "eventgen-logs")
            env["EVENTGEN_LOG_DIR"] = log_dir
        os.makedirs(env["EVENTGEN_LOG_DIR"], exist_ok=True)
        return env

    def _command(self):
        # type: () -> List[str]
        return build_command(self._conf_path)

    def start(self):
        # type: () -> None
        if self.is_alive():
            raise EngineError("engine already running")
        cmd = self._command()
        log.info("starting engine: %s", " ".join(cmd))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._build_env(),
                cwd=self._cwd,
                text=True,
                bufsize=1,
                start_new_session=True,  # our SIGTERM must not hit the engine
            )
        except OSError as exc:
            raise EngineError("failed to start engine %r: %s" % (cmd, exc))
        self._reader = threading.Thread(
            target=self._pump_output, args=(self._proc,),
            name="stoker-engine-log", daemon=True)
        self._reader.start()

    def _pump_output(self, proc):
        # type: (subprocess.Popen) -> None
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                with self._ring_lock:
                    self._ring.append(line)
                log.debug("engine: %s", line)
        except (ValueError, OSError):
            pass  # stream closed during stop
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

    def stop(self, grace_s=STOP_GRACE_S):
        # type: (float) -> Optional[int]
        proc = self._proc
        if proc is None:
            return None
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(grace_s)
            except subprocess.TimeoutExpired:
                log.warning("engine ignored SIGTERM for %.0f s; killing", grace_s)
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(5.0)
                except subprocess.TimeoutExpired:
                    log.error("engine unkillable; abandoning")
        if self._reader is not None:
            self._reader.join(2.0)
        return proc.poll()

    def restart(self):
        # type: () -> None
        """Retarget-beyond-headroom path: SIGTERM, wait, respawn."""
        log.info("restarting engine (retarget beyond headroom)")
        self.stop()
        self.start()

    def is_alive(self):
        # type: () -> bool
        return self._proc is not None and self._proc.poll() is None

    @property
    def returncode(self):
        # type: () -> Optional[int]
        return self._proc.poll() if self._proc is not None else None

    def log_tail(self):
        # type: () -> List[str]
        with self._ring_lock:
            return list(self._ring)


class RawReplayRunner(EngineRunner):
    """Subprocess manager for the PISTON raw-replay engine.

    Reuses every bit of :class:`EngineRunner` (Popen, the daemon log-reader ring
    buffer, SIGTERM->grace->SIGKILL stop, is_alive/returncode/log_tail) and only
    changes what makes an engine an engine: the command (``python -m
    stoker_rawreplay``, no conf argument), the PYTHONPATH root (the rawreplay
    package tree, not the eventgen tree) and the environment (the STOKER_RAWREPLAY_*
    contract instead of eventgen's log dir).

    Construction takes the dataset path + mode + time_multiple (and optional
    cadence timestamp hints) rather than a conf path. ``cwd`` is rooted at the
    pack like the eventgen runner (harmless for rawreplay, which uses an absolute
    dataset path, but kept for symmetry and any relative pack references).
    """

    def __init__(self, socket_path, dataset, mode, time_multiple=1.0,
                 ts_field=None, ts_regex=None, ts_strptime=None,
                 fallback_gap_s=None, engine_root=None, extra_env=None,
                 ring_size=DEFAULT_RING_SIZE, cwd=None, log_dir=None):
        # type: (str, str, str, float, Optional[str], Optional[str], Optional[str], Optional[float], Optional[str], Optional[Dict[str, str]], int, Optional[str], Optional[str]) -> None
        # The base uses conf_path only to derive a default log directory; pass the
        # explicit log_dir (or the dataset's dir) so that dirname() is valid.
        conf_stand_in = log_dir or os.path.dirname(dataset) or "."
        EngineRunner.__init__(
            self, conf_stand_in, socket_path,
            engine_root=engine_root or DEFAULT_RAWREPLAY_ROOT,
            extra_env=extra_env, ring_size=ring_size, cwd=cwd)
        self._dataset = dataset
        self._mode = mode
        self._time_multiple = time_multiple
        self._ts_field = ts_field
        self._ts_regex = ts_regex
        self._ts_strptime = ts_strptime
        self._fallback_gap_s = fallback_gap_s

    def _command(self):
        # type: () -> List[str]
        return build_rawreplay_command()

    def _build_env(self):
        # type: () -> Dict[str, str]
        env = dict(os.environ)
        env.update(self._extra_env)
        env["STOKER_OUTPUT_SOCKET"] = self._socket_path
        # PYTHONPATH: prepend the rawreplay package root so `-m stoker_rawreplay`
        # resolves (same mechanism as the eventgen runner prepends its tree).
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = self._engine_root + (
            os.pathsep + existing if existing else "")
        # The rawreplay contract (see stoker_rawreplay.engine.load_config and
        # docs/WORKER-CONTRACT.md). The dataset path is absolute (bundle.py
        # absolutises it and rejects escapes); never a secret.
        env["STOKER_RAWREPLAY_DATASET"] = self._dataset
        env["STOKER_RAWREPLAY_MODE"] = self._mode
        env["STOKER_RAWREPLAY_TIME_MULTIPLE"] = repr(float(self._time_multiple))
        if self._ts_field:
            env["STOKER_RAWREPLAY_TS_FIELD"] = self._ts_field
        if self._ts_regex:
            env["STOKER_RAWREPLAY_TS_REGEX"] = self._ts_regex
        if self._ts_strptime:
            env["STOKER_RAWREPLAY_TS_STRPTIME"] = self._ts_strptime
        if self._fallback_gap_s is not None:
            env["STOKER_RAWREPLAY_FALLBACK_GAP_S"] = repr(float(self._fallback_gap_s))
        return env


class MetricsRunner(EngineRunner):
    """Subprocess manager for the metrics engine (``python -m stoker_metrics``).

    Reuses every bit of :class:`EngineRunner` (Popen, log-reader ring buffer,
    SIGTERM->grace->SIGKILL stop) and only changes the command, the PYTHONPATH
    root (the metrics package tree) and the environment (the STOKER_METRICS_*
    contract). Construction takes the resolved config-file path (the agent writes
    the pack's ``metrics:`` block there), this worker's slot / total_workers (the
    engine strides the series matrix by them) and an optional resolution override.
    """

    def __init__(self, socket_path, config_path, slot, total_workers,
                 resolution_s=None, backfill_start_s=None, backfill_end_s=None,
                 backfill_resolution_s=None, engine_root=None, extra_env=None,
                 ring_size=DEFAULT_RING_SIZE, cwd=None, log_dir=None):
        # type: (str, str, int, int, Optional[float], Optional[float], Optional[float], Optional[float], Optional[str], Optional[Dict[str, str]], int, Optional[str], Optional[str]) -> None
        conf_stand_in = log_dir or os.path.dirname(config_path) or "."
        EngineRunner.__init__(
            self, conf_stand_in, socket_path,
            engine_root=engine_root or DEFAULT_METRICS_ROOT,
            extra_env=extra_env, ring_size=ring_size, cwd=cwd)
        self._config_path = config_path
        self._slot = slot
        self._total_workers = total_workers
        self._resolution_s = resolution_s
        self._backfill_start_s = backfill_start_s
        self._backfill_end_s = backfill_end_s
        self._backfill_resolution_s = backfill_resolution_s

    def _command(self):
        # type: () -> List[str]
        return build_metrics_command()

    def _build_env(self):
        # type: () -> Dict[str, str]
        env = dict(os.environ)
        env.update(self._extra_env)
        env["STOKER_OUTPUT_SOCKET"] = self._socket_path
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = self._engine_root + (
            os.pathsep + existing if existing else "")
        env["STOKER_METRICS_CONFIG"] = self._config_path
        env["STOKER_METRICS_SLOT"] = str(int(self._slot))
        env["STOKER_METRICS_TOTAL_WORKERS"] = str(int(self._total_workers))
        if self._resolution_s is not None:
            env["STOKER_METRICS_RESOLUTION_S"] = repr(float(self._resolution_s))
        if self._backfill_start_s is not None and self._backfill_end_s is not None:
            env["STOKER_METRICS_BACKFILL_START_S"] = repr(float(self._backfill_start_s))
            env["STOKER_METRICS_BACKFILL_END_S"] = repr(float(self._backfill_end_s))
            if self._backfill_resolution_s is not None:
                env["STOKER_METRICS_BACKFILL_RESOLUTION_S"] = repr(
                    float(self._backfill_resolution_s))
        return env
