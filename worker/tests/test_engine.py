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
