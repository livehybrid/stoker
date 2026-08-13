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

:class:`~server.drivers.inprocess.InProcessDriver` subclasses the spawn mode to
run small workloads inside the control-plane container itself; the
``capture_logs`` option and the ``_slot_env`` hook below exist for it.
"""

from __future__ import annotations

import itertools
import logging
import os
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Set

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


# Bound on captured worker output kept per fleet (oldest lines dropped), so a
# chatty long run cannot grow control-plane memory without limit.
_LOG_CAP_LINES = 4000


class FakeDriver(object):
    """A complete in-memory driver. Thread-safe; needs no external services."""

    def __init__(self, spawn=False, python_executable=None, cwd=None,
                 env_overrides=None, capture_logs=False):
        # type: (bool, Optional[str], Optional[str], Optional[Dict[str, str]], bool) -> None
        """
        Args:
            spawn: when True, ``create`` launches real ``stoker_agent``
                subprocesses (managed mode). When False (default), the driver is
                pure bookkeeping and reports desired == running.
            python_executable: interpreter for spawned workers (defaults to the
                current one).
            cwd: working directory for spawned workers.
            env_overrides: extra env for spawned workers (merged over os.environ
                and the run snapshot env — an override always wins).
            capture_logs: when True (spawn mode), each worker's stdout+stderr is
                read by a daemon thread into the fleet's bounded ``log_lines``
                ring so ``logs()`` serves real output; when False output goes to
                /dev/null (the historical test behaviour).
        """
        self._spawn = spawn
        self._python = python_executable or sys.executable
        self._cwd = cwd
        self._env_overrides = dict(env_overrides or {})
        self._capture_logs = capture_logs
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
            state = self._resolve_state(ref)
            if state is None:
                log.info("fake driver destroy: fleet %s already gone", ref.id)
                return
            state.destroyed = True
            state.desired = 0
        if self._spawn and state is not None:
            self._terminate_procs(state, grace_s=1)
        log.info("fake driver destroyed fleet %s (run %s)", ref.id, state.run_id)

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

    # -- discovery (optional 7th method) ---------------------------------- #

    def list_run_ids(self):
        # type: () -> Set[int]
        """Return the run ids of every live (non-destroyed) in-memory fleet.

        The boot stray-sweep analogue for the in-process driver: a fleet whose
        state has been destroyed is gone (its workload no longer exists), so it
        is excluded, mirroring what a swarm/k8s backend would no longer list.
        """
        with self._lock:
            return {
                state.run_id
                for state in self._fleets.values()
                if not state.destroyed
            }

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

    def _resolve_state(self, ref):
        # type: (DriverRef) -> Optional[_FleetState]
        """Find a fleet by its native id, else by ``raw['run_id']`` (caller holds
        the lock).

        The native fleet id is opaque (``fake-run-<run>-<n>``) and cannot be
        reconstructed from a run id alone, so the boot stray-sweep synthesises a
        destroy ref carrying only ``raw={'run_id': <id>}``. Falling back to a
        run-id match lets that synthesised ref destroy the right in-memory fleet,
        mirroring how swarm/k8s address a stray by its ``stoker-run-<id>`` name.
        """
        state = self._fleets.get(ref.id)
        if state is not None:
            return state
        run_id = (ref.raw or {}).get("run_id")
        if run_id is None:
            return None
        for candidate in self._fleets.values():
            if candidate.run_id == run_id and not candidate.destroyed:
                return candidate
        return None

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

    def _child_env(self, state, slot):
        # type: (_FleetState, int) -> Dict[str, str]
        """The env for one spawned worker: process env < snapshot < overrides
        < per-slot values, plus the slot hint. Precedence matters: the driver's
        overrides (e.g. a localhost control URL) must beat the snapshot's."""
        env = dict(os.environ)
        env.update(state.snapshot.env)
        env.update(self._env_overrides)
        env.update(self._slot_env(state, slot))
        # The worker reads its slot hint from STOKER_HINT_SLOT.
        env.setdefault("STOKER_HINT_SLOT", str(slot))
        return env

    def _slot_env(self, state, slot):
        # type: (_FleetState, int) -> Dict[str, str]
        """Per-slot env hook (empty here). InProcessDriver overrides it to give
        each worker its own unix output socket — every spawned worker shares
        one filesystem, so any fixed default path would collide."""
        return {}

    def _spawn_one(self, state, slot):
        # type: (_FleetState, int) -> None
        env = self._child_env(state, slot)
        sink = subprocess.PIPE if self._capture_logs else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                [self._python, "-m", "stoker_agent"],
                env=env, cwd=self._cwd,
                stdout=sink, stderr=subprocess.STDOUT if self._capture_logs
                else subprocess.DEVNULL,
            )
        except OSError as exc:
            raise DriverError("failed to spawn worker slot %d: %s" % (slot, exc))
        state.procs.append(proc)
        if self._capture_logs and proc.stdout is not None:
            self._start_log_reader(state, slot, proc)
        log.info("fake driver spawned worker pid=%d slot=%d run=%d",
                 proc.pid, slot, state.run_id)

    def _start_log_reader(self, state, slot, proc):
        # type: (_FleetState, int, subprocess.Popen) -> None
        """Daemon thread streaming one worker's output into the bounded ring.

        Reading (rather than /dev/null) also stops the child blocking on a full
        pipe. The thread exits at EOF (process end); daemon=True so it never
        pins interpreter shutdown.
        """
        def _pump():
            # type: () -> None
            try:
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    with self._lock:
                        state.log_lines.append("[slot %d] %s" % (slot, line))
                        if len(state.log_lines) > _LOG_CAP_LINES:
                            del state.log_lines[:-_LOG_CAP_LINES]
            except (OSError, ValueError):
                pass  # pipe closed mid-read (terminate/kill); nothing to keep
            finally:
                try:
                    proc.stdout.close()
                except OSError:
                    pass

        threading.Thread(
            target=_pump, name="stoker-worker-log-%d-%d" % (state.run_id, slot),
            daemon=True).start()

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
