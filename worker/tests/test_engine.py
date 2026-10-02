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


# --------------------------------------------------------------------------- #
# Engine implementation selection: firebox (Rust) by default when its binary is
# present, the vendored Python eventgen otherwise or when forced.

import stat as _stat

from stoker_agent.engine import EngineError, build_command, eventgen_impl


def _fake_firebox(tmp_path):
    binary = tmp_path / "firebox"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | _stat.S_IXUSR)
    return str(binary)


def test_build_command_uses_firebox_when_binary_named(tmp_path):
    binary = _fake_firebox(tmp_path)
    env = {"STOKER_FIREBOX_BIN": binary}
    assert build_command("/w/eventgen.conf", env) == [binary, "-v", "generate", "/w/eventgen.conf"]
    assert eventgen_impl(env) == ("firebox", binary)


def test_build_command_finds_firebox_on_path(tmp_path):
    binary = _fake_firebox(tmp_path)
    env = {"PATH": str(tmp_path)}
    assert build_command("/w/eventgen.conf", env)[0] == binary


def test_build_command_auto_falls_back_to_python(tmp_path):
    env = {"PATH": str(tmp_path)}  # empty dir: no firebox anywhere
    cmd = build_command("/w/eventgen.conf", env)
    assert cmd[1:] == ["-m", "splunk_eventgen", "generate", "/w/eventgen.conf"]
    assert eventgen_impl(env) == ("python", None)


def test_build_command_python_forced_ignores_binary(tmp_path):
    binary = _fake_firebox(tmp_path)
    env = {"STOKER_FIREBOX_BIN": binary, "STOKER_EVENTGEN_IMPL": "python"}
    assert build_command("/w/eventgen.conf", env)[1:3] == ["-m", "splunk_eventgen"]


def test_build_command_firebox_forced_without_binary_fails(tmp_path):
    env = {"PATH": str(tmp_path), "STOKER_EVENTGEN_IMPL": "firebox"}
    try:
        build_command("/w/eventgen.conf", env)
    except EngineError as exc:
        assert "firebox" in str(exc)
    else:
        raise AssertionError("expected EngineError")


def test_build_command_rejects_unknown_impl():
    try:
        build_command("/w/eventgen.conf", {"STOKER_EVENTGEN_IMPL": "cobol"})
    except EngineError:
        pass
    else:
        raise AssertionError("expected EngineError")


def test_build_command_non_executable_explicit_binary_is_ignored(tmp_path):
    binary = tmp_path / "firebox"
    binary.write_text("not executable")
    env = {"STOKER_FIREBOX_BIN": str(binary), "PATH": str(tmp_path)}
    assert eventgen_impl(env) == ("python", None)


def test_build_command_threads_passthrough(tmp_path):
    binary = _fake_firebox(tmp_path)
    env = {"STOKER_FIREBOX_BIN": binary, "STOKER_FIREBOX_THREADS": "4"}
    assert build_command("/w/eventgen.conf", env)[-2:] == ["--threads", "4"]


def test_engine_cmd_override_beats_impl_selection(tmp_path):
    binary = _fake_firebox(tmp_path)
    env = {"STOKER_FIREBOX_BIN": binary, "STOKER_ENGINE_CMD": "/bin/echo {conf} x"}
    assert build_command("/w/eventgen.conf", env) == ["/bin/echo", "/w/eventgen.conf", "x"]


# --------------------------------------------------------------------------- #
# Identity rotation needs firebox
# --------------------------------------------------------------------------- #

def _rotating_conf(tmp_path, rotate=True):
    path = tmp_path / "eventgen.conf"
    body = ["[s.sample]", "mode = sample", "interval = 1", "count = -1",
            "token.0.token = user=(\\d+)"]
    body.append("token.0.replacementType = %s" % ("rotate" if rotate else "static"))
    body.append("token.0.replacement = keep" if rotate else "token.0.replacement = x")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return str(path)


def test_rotation_refuses_the_python_engine(tmp_path):
    """The vendored eventgen drops an unknown replacementType silently.

    It would emit the stable pseudonym, so nothing leaks, but nothing rotates
    either and nothing says so: every replay would reuse one identity and the
    cardinality the pack was built for would simply not appear. Failing the run
    is the only honest outcome, and the message has to say what to do.
    """
    conf = _rotating_conf(tmp_path)
    with pytest.raises(EngineError) as exc:
        build_command(conf, {"STOKER_EVENTGEN_IMPL": "python"})
    text = str(exc.value)
    assert "only firebox" in text
    assert "turn rotation off" in text


def test_a_pack_that_does_not_rotate_still_runs_on_python(tmp_path):
    conf = _rotating_conf(tmp_path, rotate=False)
    cmd = build_command(conf, {"STOKER_EVENTGEN_IMPL": "python"})
    assert cmd[-1] == conf and "splunk_eventgen" in cmd


def test_rotation_runs_on_firebox(tmp_path):
    binary = tmp_path / "firebox"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    conf = _rotating_conf(tmp_path)
    cmd = build_command(conf, {"STOKER_FIREBOX_BIN": str(binary)})
    assert cmd[0] == str(binary)


def test_rotation_detection_ignores_comments_and_other_types(tmp_path):
    path = tmp_path / "c.conf"
    path.write_text(
        "[s]\n# token.9.replacementType = rotate\n"
        "token.0.replacementType = static\n"
        "token.1.replacement = rotate\n",   # a VALUE of rotate is not a type
        encoding="utf-8")
    from stoker_agent.engine import conf_declares_rotation
    assert conf_declares_rotation(str(path)) is False
    assert conf_declares_rotation(str(tmp_path / "missing.conf")) is False
