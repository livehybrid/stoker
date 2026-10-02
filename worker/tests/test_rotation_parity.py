"""Identity rotation, from the pack builder through the real engine.

Every other rotation test checks one side: the builder's own tests assert what
it writes, firebox's assert what it replays. These assert that the two agree,
which is the only property an operator actually depends on.

They live here, not in ``server/tests``, for one reason: this is the suite CI
runs **after** it builds firebox. In ``server/tests`` they would find no binary
and skip, which would read as passing while proving nothing. ``packbuilder`` is
stdlib-only, so importing it here costs no dependency.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

WORKER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(WORKER)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from server import packbuilder as pb  # noqa: E402  (stdlib-only module)
from server.packbuilder import pseudonym as ps  # noqa: E402

SUB = ps.derive_subkey(bytes(range(32)))
JOURNEY = ["user=483920 action=login",
           "user=771045 action=register",
           "user=483920 action=logout"]


def _firebox():
    # type: () -> str
    import shutil
    for cand in (os.environ.get("STOKER_FIREBOX_BIN"), os.environ.get("FIREBOX_BIN"),
                 shutil.which("firebox"),
                 os.path.join(WORKER, "engines", "firebox", "target",
                              "x86_64-unknown-linux-musl", "release", "firebox"),
                 "/opt/aios/apps/firebox/target/x86_64-unknown-linux-musl/release/firebox"):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    pytest.skip("firebox binary not built; set FIREBOX_BIN")


def _cfg(scope="pass", period=None, widen=None, events=None, tokens=None):
    rep = {"kind": "pseudonym", "rotate": True}
    if widen is not None:
        rep["widen"] = widen
    cfg = {"name": "Sessions", "order": "sequential", "count": -1, "interval": 1,
           "events": list(events or JOURNEY),
           "tokens": tokens or [{"field": "user", "pattern": r"user=(\d+)",
                                 "replacement": rep}],
           "rotation": {"scope": scope}}
    if period is not None:
        cfg["rotation"]["period"] = period
    return cfg


def _build_and_run(tmp_path, cfg, name, intervals=3, workers="1", slot="0"):
    """Write the pack as the builder does, then replay it as a worker does."""
    clean = pb.validate_config(cfg)
    out = pb.write_pack(clean, os.path.join(str(tmp_path), name),
                        subkey=SUB, fingerprint="fp123")
    conf = os.path.join(out, "default", "eventgen.conf")
    with open(conf, encoding="utf-8") as fh:
        text = fh.read()
    # The agent points output at its socket and sets the fleet position; print
    # instead so the test can read the events.
    with open(conf, "w", encoding="utf-8") as fh:
        fh.write("%soutputMode = stdout\nend = %d\n" % (text, intervals))
    done = subprocess.run([_firebox(), "generate", "default/eventgen.conf"], cwd=out,
                          timeout=180, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=dict(os.environ, STOKER_ROTATE_WORKERS=workers,
                                   STOKER_ROTATE_SLOT=slot))
    assert done.returncode == 0, done.stderr.decode()[-2000:]
    lines = [l for l in done.stdout.decode().splitlines() if "user=" in l or "src=" in l]
    return clean, out, lines


def _users(lines):
    return [re.search(r"user=(\d+)", l).group(1) for l in lines]


def test_each_replay_is_a_new_user_whose_journey_still_joins(tmp_path):
    """The customer's actual request, end to end.

    A stable stand-in replays one user logging in for ever, which makes
    anything that counts or groups by user meaningless. Each pass must be a new
    user, and the login and logout of that pass must still belong together.
    """
    clean, out, lines = _build_and_run(tmp_path, _cfg(), "pass")
    assert len(lines) == 9, lines
    with open(os.path.join(out, "samples", "sessions.sample"), encoding="utf-8") as fh:
        stand_ins = set(re.findall(r"user=(\d+)", fh.read()))
    assert len(stand_ins) == 2

    journeys = {}
    for line in lines:
        journeys.setdefault(re.search(r"user=(\d+)", line).group(1), []).append(
            re.search(r"action=(\w+)", line).group(1))
    # Neither the originals nor the un-rotated stand-ins reach the output.
    assert not {"483920", "771045"} & set(journeys)
    assert not stand_ins & set(journeys), "the stand-ins were not rotated"
    assert len(journeys) == 6, journeys
    assert sorted(sorted(v) for v in journeys.values()) == [
        ["login", "logout"], ["login", "logout"], ["login", "logout"],
        ["register"], ["register"], ["register"]]
    for user in journeys:
        assert len(user) == 6 and user.isdigit(), user


def test_aligned_rotation_holds_one_identity_for_the_window(tmp_path):
    _clean, _out, lines = _build_and_run(tmp_path, _cfg(scope="window", period=3600),
                                         "window")
    assert len(lines) == 9
    counts = {}
    for user in _users(lines):
        counts[user] = counts.get(user, 0) + 1
    # One hour window, so all three replays share its identities: two users,
    # one of whom logs in and out three times.
    assert sorted(counts.values()) == [3, 6], counts


@pytest.mark.parametrize("widen", [None, {"shape": "digits", "length": 15}])
def test_the_preview_shows_what_the_engine_will_send(tmp_path, widen):
    """The preview derives (k, D) itself; the engine derives them from the sample.

    If those two walks ever disagree the preview would quietly show identities
    the run never produces, which is worse than showing none: the operator
    would sign off on a correlation that does not exist. So they are compared
    against the real binary rather than against each other's code.
    """
    cfg = _cfg(widen=widen)
    previewed = pb.render_preview(pb.validate_config(cfg), 9, seed=1, subkey=SUB)["events"]
    _clean, _out, produced = _build_and_run(tmp_path, cfg, "agree-%s" % bool(widen))
    assert len(produced) == 9
    assert _users(previewed) == _users(produced), \
        "the preview and the engine disagree on the rotated identities"


def test_two_rotating_fields_share_one_identity_table(tmp_path):
    """`src=X` and `dst=X` must rotate to the same new identity.

    The engine keeps one table per format class for the whole stanza, so a
    value matched by two patterns is one identity: a pack with src_user and
    dest_user has to keep joining after it rotates. A table per field would
    also give a different D, and D is in the identity, so every value would
    differ from what the run sends.
    """
    cfg = _cfg(events=["src=483920 dst=771045 act=call",
                       "src=771045 dst=483920 act=reply"],
               tokens=[{"field": "src", "pattern": r"src=(\d+)",
                        "replacement": {"kind": "pseudonym", "rotate": True}},
                       {"field": "dst", "pattern": r"dst=(\d+)",
                        "replacement": {"kind": "pseudonym", "rotate": True}}])
    clean = pb.validate_config(cfg)
    assert clean["rotation"]["fields"] == ["src", "dst"]
    previewed = pb.render_preview(clean, 4, seed=1, subkey=SUB)["events"]
    _clean, _out, produced = _build_and_run(tmp_path, cfg, "pairs", intervals=2)
    assert len(produced) == 4

    def pairs(lines):
        return [(re.search(r"src=(\d+)", l).group(1), re.search(r"dst=(\d+)", l).group(1))
                for l in lines]

    assert pairs(previewed) == pairs(produced), \
        "the preview and the engine disagree once two fields share a table"
    # The property itself: within a pass the two events are the same two
    # parties, swapped. One identity, two fields.
    first, second = pairs(produced)[0], pairs(produced)[1]
    assert first == (second[1], second[0]), pairs(produced)
    assert set(pairs(produced)[:2]).isdisjoint(set(pairs(produced)[2:]))


def test_every_worker_slot_mints_its_own_identities(tmp_path):
    """The property that matters at the customer's 58 slots.

    Every worker walks the same event ordinals; it is the slot digit alone that
    stops two of them sending the same identity. Checked through the built pack
    and the real binary, because a mistake here is invisible in the data.
    """
    cfg = _cfg(widen={"shape": "digits", "length": 12})
    seen = {}
    for slot in range(4):
        _c, _o, lines = _build_and_run(tmp_path, cfg, "slot%d" % slot,
                                       workers="4", slot=str(slot))
        for user in _users(lines):
            assert seen.setdefault(user, slot) == slot, \
                "identity %s minted by two slots" % user
    assert len(seen) >= 20, len(seen)
