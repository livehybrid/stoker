"""EngineRunner subprocess wiring.

The engine is launched with a working directory rooted at the pack so
eventgen resolves relative file-token replacement paths (e.g.
`samples/status_codes.sample`) against the pack rather than the container
working directory (regression: confrewrite#2). Popen is faked so these
tests do not need the vendored engine.
"""

import io

import stoker_agent.engine as engine_mod
from stoker_agent.engine import EngineRunner


class _FakePopen:
    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdout = io.StringIO("")  # empty -> the log reader exits at once
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False


def _patch_popen(monkeypatch):
    calls = {}

    def fake_popen(cmd, **kwargs):
        calls["cwd"] = kwargs.get("cwd")
        return _FakePopen(cmd, **kwargs)

    monkeypatch.setattr(engine_mod.subprocess, "Popen", fake_popen)
    return calls


def test_engine_launches_in_given_cwd(tmp_path, monkeypatch):
    calls = _patch_popen(monkeypatch)
    runner = EngineRunner(str(tmp_path / "eventgen.conf"),
                          str(tmp_path / "out.sock"),
                          cwd=str(tmp_path))
    runner.start()
    try:
        assert calls["cwd"] == str(tmp_path)
    finally:
        runner.stop()


def test_engine_default_cwd_is_none(tmp_path, monkeypatch):
    calls = _patch_popen(monkeypatch)
    runner = EngineRunner(str(tmp_path / "eventgen.conf"),
                          str(tmp_path / "out.sock"))
    runner.start()
    try:
        assert calls["cwd"] is None  # inherit: the pre-fix behaviour
    finally:
        runner.stop()


# --------------------------------------------------------------------------- #
# Process-group teardown: eventgen forks worker children into the engine's
# session group. stop() must reap the WHOLE group, or a child orphans onto the
# workdir the agent then deletes and crash-loops at 0 eps (the "some workers
# emit nothing" bug). These use a REAL subprocess (no faked Popen).
# --------------------------------------------------------------------------- #

import os as _os
import signal as _signal
import sys as _sys
import time as _time

import pytest


def _alive(pid):
    try:
        _os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, not ours (won't happen in-test)
        return True


# A fake engine that forks a SIGTERM-ignoring child (like an eventgen worker),
# records both pids, and — as the group leader — exits cleanly on SIGTERM. So a
# plain proc.terminate() reaps the leader but ORPHANS the child; only a group
# kill clears it. argv[1] is the "conf" path, which we set to the pidfile.
_FAKE_ENGINE = (
    "import os, signal, sys, time\n"
    "pidfile = sys.argv[1]\n"
    "pid = os.fork()\n"
    "if pid == 0:\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    time.sleep(120)\n"
    "    os._exit(0)\n"
    "signal.signal(signal.SIGTERM, lambda *a: os._exit(0))\n"
    "open(pidfile, 'w').write('%d %d' % (os.getpid(), pid))\n"
    "time.sleep(120)\n"
)


@pytest.mark.skipif(not hasattr(_os, "fork") or not hasattr(_os, "killpg"),
                    reason="needs POSIX fork + process groups")
def test_stop_reaps_the_whole_engine_group(tmp_path, monkeypatch):
    pidfile = tmp_path / "pids.txt"
    script = tmp_path / "fake_engine.py"
    script.write_text(_FAKE_ENGINE)
    monkeypatch.setenv("STOKER_ENGINE_CMD", "%s %s" % (_sys.executable, script))

    # conf_path == pidfile: build_command appends it as the script's argv[1].
    runner = EngineRunner(str(pidfile), str(tmp_path / "out.sock"),
                          cwd=str(tmp_path))
    runner.start()
    deadline = _time.time() + 10
    while _time.time() < deadline and not pidfile.exists():
        _time.sleep(0.05)
    assert pidfile.exists(), "fake engine never wrote its pids"
    parent_pid, child_pid = (int(x) for x in pidfile.read_text().split())
    assert _alive(parent_pid) and _alive(child_pid)

    runner.stop(grace_s=2.0)

    deadline = _time.time() + 5
    while _time.time() < deadline and (_alive(parent_pid) or _alive(child_pid)):
        _time.sleep(0.05)
    assert not _alive(parent_pid), "engine leader survived stop()"
    # The load-bearing assertion: the SIGTERM-ignoring child (an orphaned
    # eventgen worker) is gone because stop() killed the whole group.
    assert not _alive(child_pid), "orphaned engine child survived stop()"
