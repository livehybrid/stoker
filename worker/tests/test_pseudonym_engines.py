"""Consistent pseudonyms, through the real engines.

The control-plane tests prove the stand-ins are right. This proves the part that
only a real engine can: that a pack carrying them **replays identically on
firebox and on the vendored Python eventgen**, preserves the correlation the
operator needs, and contains none of the original identifiers.

That property is the whole argument for doing this at build time rather than as
a new ``replacementType``. The sample text IS the stand-ins, so there is nothing
for an engine to compute, nothing to disagree about, and nothing an engine that
has never heard of the feature can drop. Both engines silently ignore a
``replacementType`` they do not know and emit the matched text verbatim with
exit 0, so the engine-side alternative would have leaked real identifiers on any
un-patched worker.

``pseudonym.py`` is stdlib-only on purpose, so this suite can import it without
the control plane's dependencies, which the CI ``test`` job does not install.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

WORKER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(WORKER)
ENGINE_DIR = os.path.join(WORKER, "engines", "eventgen")
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from server.packbuilder import pseudonym as ps  # noqa: E402  (stdlib-only module)

KEY = bytes(range(32))
SUB = ps.derive_subkey(KEY)

# The customer's own sample: the journey must still join after a replay.
ORIGINALS = [
    ("123", "login"),
    ("456", "register"),
    ("123", "logout"),
]


def _firebox():
    # type: () -> str
    for candidate in (os.environ.get("STOKER_FIREBOX_BIN"),
                      os.environ.get("FIREBOX_BIN"),
                      shutil.which("firebox"),
                      os.path.join(WORKER, "engines", "firebox", "target",
                                   "x86_64-unknown-linux-musl", "release", "firebox"),
                      "/opt/aios/apps/firebox/target/x86_64-unknown-linux-musl/release/firebox"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    pytest.skip("firebox binary not built; set FIREBOX_BIN")


def _pack(tmp_path, intervals=3):
    # type: (object, int) -> str
    """A pack whose sample already holds stand-ins, as the builder writes it."""
    pack = os.path.join(str(tmp_path), "pack")
    os.makedirs(os.path.join(pack, "default"))
    os.makedirs(os.path.join(pack, "samples"))
    lines = []
    for original, action in ORIGINALS:
        stand_in = ps.pseudonym(original, SUB)
        assert stand_in is not None
        lines.append("user=%s action=%s" % (stand_in, action))
    with open(os.path.join(pack, "samples", "sessions.sample"), "w") as handle:
        handle.write("\n".join(lines) + "\n")
    with open(os.path.join(pack, "default", "eventgen.conf"), "w") as handle:
        handle.write(
            "[sessions.sample]\n"
            "mode = sample\n"
            "interval = 1\n"
            "count = -1\n"
            "earliest = -1s\n"
            "latest = now\n"
            "outputMode = stdout\n"
            "end = %d\n" % intervals)
    return pack


def _run(engine, pack):
    # type: (str, str) -> list
    conf = os.path.join("default", "eventgen.conf")
    if engine == "firebox":
        cmd = [_firebox(), "generate", conf]
        env = dict(os.environ)
    else:
        cmd = [sys.executable, "-m", "splunk_eventgen", "generate", conf]
        env = dict(os.environ, PYTHONPATH=ENGINE_DIR)
    done = subprocess.run(cmd, cwd=pack, env=env, timeout=180,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert done.returncode == 0, done.stderr.decode()[-2000:]
    return [l for l in done.stdout.decode().splitlines() if "user=" in l]


def test_both_engines_replay_the_same_stand_ins(tmp_path):
    pytest.importorskip("dateutil")
    pack = _pack(tmp_path)
    firebox_lines = _run("firebox", pack)
    python_lines = _run("python", pack)

    assert firebox_lines, "firebox generated nothing"
    assert sorted(firebox_lines) == sorted(python_lines), (
        "the two engines must replay a pseudonymised pack identically")

    for lines in (firebox_lines, python_lines):
        # 3 sample events x 3 intervals, because count = -1 is the whole sample
        assert len(lines) == 9
        # No original survives anywhere.
        assert not re.search(r"user=(123|456)\b", "\n".join(lines))
        # The correlation holds: one stand-in does login AND logout, and the
        # other only registers.
        by_user = {}
        for line in lines:
            user = re.search(r"user=(\S+)", line).group(1)
            action = re.search(r"action=(\S+)", line).group(1)
            by_user.setdefault(user, set()).add(action)
        assert len(by_user) == 2, by_user
        journeys = sorted(sorted(v) for v in by_user.values())
        assert journeys == [["login", "logout"], ["register"]], by_user


def test_the_stand_ins_are_the_control_planes_own(tmp_path):
    """The engines are replaying exactly what the builder computed.

    If this drifts, a pack built on one Stoker and replayed by another version
    of the worker would stop correlating, so it is asserted rather than assumed.
    """
    pytest.importorskip("dateutil")
    expected = {ps.pseudonym(original, SUB) for original, _action in ORIGINALS}
    assert len(expected) == 2
    for engine in ("firebox", "python"):
        seen = {re.search(r"user=(\S+)", line).group(1)
                for line in _run(engine, _pack(tmp_path / engine, intervals=1))}
        assert seen == expected, engine


def test_every_worker_slot_produces_the_same_stand_in(tmp_path):
    """A run is sharded over N workers; a stand-in must not depend on the slot.

    Mode 1 gets this for free because the stand-ins are literal sample text, but
    the property is what the customer's correlation depends on at 58 slots, so
    it is checked rather than reasoned about: the conf is rewritten per slot
    exactly as the agent does it, and every slot must emit the same values.
    """
    pytest.importorskip("dateutil")
    from stoker_agent import confrewrite

    pack = _pack(tmp_path, intervals=1)
    src = os.path.join(pack, "default", "eventgen.conf")
    seen = []
    for slot in range(3):
        dst = os.path.join(pack, "default", "slot%d.conf" % slot)
        confrewrite.rewrite_file(src, dst, "count_interval", None, 1.0,
                                 os.path.join(pack, "samples"),
                                 slot=slot, total_workers=3)
        with open(dst) as handle:
            rewritten = handle.read()
        # The whole-sample count must survive the per-slot split: this is the
        # one count _rewrite_count_interval leaves alone, so every slot emits
        # the whole sample instead of a shard of it.
        assert "count = -1" in rewritten
        # The agent points output at its unix socket, which is right for a real
        # run; print instead so this test can read the events.
        rewritten = re.sub(r"(?m)^outputMode = .*$", "outputMode = stdout", rewritten)
        if "outputMode" not in rewritten:
            rewritten += "outputMode = stdout\n"
        with open(dst, "w") as handle:
            handle.write(rewritten + "end = 1\n")
        done = subprocess.run(
            [_firebox(), "generate", os.path.join("default", "slot%d.conf" % slot)],
            cwd=pack, timeout=120, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert done.returncode == 0, done.stderr.decode()[-2000:]
        seen.append({re.search(r"user=(\S+)", l).group(1)
                     for l in done.stdout.decode().splitlines() if "user=" in l})
    assert seen[0] and seen[0] == seen[1] == seen[2], seen
