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


class EngineRunner(object):
    def __init__(self, conf_path, socket_path, engine_root=None,
                 extra_env=None, ring_size=DEFAULT_RING_SIZE):
        # type: (str, str, Optional[str], Optional[Dict[str, str]], int) -> None
        self._conf_path = conf_path
        self._socket_path = socket_path
        self._engine_root = engine_root or DEFAULT_ENGINE_ROOT
        self._extra_env = dict(extra_env or {})
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
