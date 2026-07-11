"""In-process ExecutionDriver for tests and local-without-swarm.

Records desired replica counts in memory and returns synthetic DriverRef /
DriverStatus. No network, no docker. Two uses:

* **conformance and operator-API tests** use the default (no-spawn) mode: the
  driver just books the desired count, and ``status`` reports it as running so
  the lifecycle can proceed without real workers.
* the **end-to-end test** may set ``spawn=True`` to launch the real
  ``stoker_agent`` as local subprocesses (managed mode, env pointed at the test
  server). That path is optional; the default is pure bookkeeping.

State is per-instance and keyed by run id so one FakeDriver can back a whole
test session. ``destroy`` is idempotent.
"""

from __future__ import annotations

import itertools
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional

from .base import DriverError, DriverRef, DriverStatus, RunSnapshot

log = logging.getLogger("stoker.driver.fake")

_KIND = "fake"
_id_counter = itertools.count(1)


class _FleetState:
    """Bookkeeping for one created fleet."""

    def __init__(self, run_id, image, workers, snapshot):
        # type: (int, str, int, RunSnapshot) -> None
        self.run_id = run_id
        self.image = image
        self.desired = workers
        self.snapshot = snapshot
        self.stopped = False
        self.destroyed = False
        self.log_lines = []  # type: List[str]
        self.procs = []  # type: List[subprocess.Popen]


class FakeDriver(object):
    """A complete in-memory driver. Thread-safe; needs no external services."""

    def __init__(self, spawn=False, python_executable=None, cwd=None, env_overrides=None):
        # type: (bool, Optional[str], Optional[str], Optional[Dict[str, str]]) -> None
        """
        Args:
            spawn: when True, ``create`` launches real ``stoker_agent``
                subprocesses (managed mode). When False (default), the driver is
                pure bookkeeping and reports desired == running.
            python_executable: interpreter for spawned workers (defaults to the
                current one).
            cwd: working directory for spawned workers.
            env_overrides: extra env for spawned workers (merged over os.environ).
        """
        self._spawn = spawn
        self._python = python_executable or sys.executable
        self._cwd = cwd
        self._env_overrides = dict(env_overrides or {})
        self._fleets = {}  # type: Dict[str, _FleetState]
        self._lock = threading.Lock()

    # -- introspection helpers (tests reach in) ---------------------------

    def desired_for(self, ref):
        # type: (DriverRef) -> int
        """Return the recorded desired replica count for a fleet (test aid)."""
        with self._lock:
            state = self._fleets.get(ref.id)
            return state.desired if state and not state.destroyed else 0

    def is_destroyed(self, ref):
        # type: (DriverRef) -> bool
        with self._lock:
            state = self._fleets.get(ref.id)
            return state is None or state.destroyed

    # -- ExecutionDriver --------------------------------------------------

    def create(self, run, workers):
        # type: (RunSnapshot, int) -> DriverRef
        if workers < 1:
            raise DriverError("workers must be >= 1")
        fleet_id = "fake-run-%d-%d" % (run.run_id, next(_id_counter))
        state = _FleetState(run.run_id, run.image, workers, run)
        with self._lock:
            self._fleets[fleet_id] = state
        log.info("fake driver created fleet %s desired=%d image=%s",
                 fleet_id, workers, run.image)
        if self._spawn:
            self._spawn_workers(state, workers)
        return DriverRef(kind=_KIND, id=fleet_id,
                         raw={"run_id": run.run_id, "image": run.image})

    def scale(self, ref, workers):
        # type: (DriverRef, int) -> None
        if workers < 0:
            raise DriverError("workers must be >= 0")
        with self._lock:
            state = self._require(ref)
            state.desired = workers
        log.info("fake driver scaled fleet %s desired=%d", ref.id, workers)
        if self._spawn:
            self._reconcile_spawn(state, workers)

    def stop(self, ref, grace_s):
        # type: (DriverRef, int) -> None
        with self._lock:
            state = self._require(ref)
            state.stopped = True
        log.info("fake driver stopped fleet %s (grace %ds)", ref.id, grace_s)
        if self._spawn:
            self._terminate_procs(state, grace_s)

    def destroy(self, ref):
        # type: (DriverRef) -> None
        # Idempotent: destroying an unknown/already-gone fleet is a no-op.
        with self._lock:
            state = self._fleets.get(ref.id)
            if state is None:
                log.info("fake driver destroy: fleet %s already gone", ref.id)
                return
            state.destroyed = True
            state.desired = 0
        if self._spawn and state is not None:
            self._terminate_procs(state, grace_s=1)
        log.info("fake driver destroyed fleet %s", ref.id)

    def status(self, ref):
        # type: (DriverRef) -> DriverStatus
        with self._lock:
            state = self._fleets.get(ref.id)
            if state is None or state.destroyed:
                return DriverStatus(desired=0, running=0, tasks=[])
            desired = 0 if state.stopped else state.desired
            if self._spawn:
                running = sum(1 for p in state.procs if p.poll() is None)
            else:
                # Pure bookkeeping: assume the fleet reached desired.
                running = desired
            tasks = [
                {"slot": i, "holder": None, "node": "fake",
                 "state": "running" if i < running else "pending"}
                for i in range(state.desired)
            ]
            return DriverStatus(desired=desired, running=running, tasks=tasks)

    def logs(self, ref, slot, tail):
        # type: (DriverRef, Optional[int], int) -> str
        with self._lock:
            state = self._fleets.get(ref.id)
            if state is None:
                return ""
            lines = state.log_lines[-tail:] if tail else state.log_lines
            return "\n".join(lines)

    # -- test-log injection ----------------------------------------------

    def append_log(self, ref, line):
        # type: (DriverRef, str) -> None
        """Append a synthetic log line to a fleet (test aid)."""
        with self._lock:
            state = self._fleets.get(ref.id)
            if state is not None:
                state.log_lines.append(line)

    # -- internals --------------------------------------------------------

    def _require(self, ref):
        # type: (DriverRef) -> _FleetState
        state = self._fleets.get(ref.id)
        if state is None or state.destroyed:
            raise DriverError("unknown fleet %r" % ref.id)
        return state

    def _spawn_workers(self, state, workers):
        # type: (_FleetState, int) -> None
        for slot in range(workers):
            self._spawn_one(state, slot)

    def _reconcile_spawn(self, state, workers):
        # type: (_FleetState, int) -> None
        current = len(state.procs)
        if workers > current:
            for slot in range(current, workers):
                self._spawn_one(state, slot)
        # Scaling down does not kill specific procs here (the lease/heartbeat
        # supersede path handles identity); tests that scale down and assert on
        # process count use the non-spawn mode.

    def _spawn_one(self, state, slot):
        # type: (_FleetState, int) -> None
        env = dict(os.environ)
        env.update(state.snapshot.env)
        env.update(self._env_overrides)
        # The worker reads its slot hint from STOKER_HINT_SLOT.
        env.setdefault("STOKER_HINT_SLOT", str(slot))
        try:
            proc = subprocess.Popen(
                [self._python, "-m", "stoker_agent"],
                env=env, cwd=self._cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise DriverError("failed to spawn worker slot %d: %s" % (slot, exc))
        state.procs.append(proc)
        log.info("fake driver spawned worker pid=%d slot=%d run=%d",
                 proc.pid, slot, state.run_id)

    def _terminate_procs(self, state, grace_s):
        # type: (_FleetState, int) -> None
        for proc in state.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in state.procs:
            try:
                proc.wait(timeout=max(1, grace_s))
            except subprocess.TimeoutExpired:
                proc.kill()


__all__ = ["FakeDriver"]
