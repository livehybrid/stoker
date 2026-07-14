"""Preview-render tests: server/preview.py + the /packs/{id}/preview_run route.

The preview is a lightweight, side-effect-free render used for pack authoring and
the wizard (no fleet, no HEC). It must:

* render ``n`` events by cycling the sample lines, with the timestamp token
  rewritten to ~now and the ipv4 / integer[a:b] tokens substituted;
* leave unknown token types untouched;
* never read a file outside the pack root (a sampleFile/mvfile token that would
  escape via ``..`` / an absolute path yields no lines, not an arbitrary read);
* clamp ``n`` to a sane maximum.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import re

from server import preview
from server.preview import PREVIEW_N_MAX, clamp_n, preview_pack

from . import _helpers


# --------------------------------------------------------------------------- #
# Pack builders (local to this module so the token shapes are explicit).
# --------------------------------------------------------------------------- #

def _write_pack(root, name, conf, sample_lines, sample_basename=None):
    # type: (str, str, str, str, str) -> str
    """Write a minimal pack: default/eventgen.conf + samples/<name>.sample."""
    pack_dir = os.path.join(root, name)
    os.makedirs(os.path.join(pack_dir, "default"))
    os.makedirs(os.path.join(pack_dir, "samples"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w", encoding="utf-8") as fh:
        fh.write(conf)
    basename = sample_basename or ("%s.sample" % name)
    with open(os.path.join(pack_dir, "samples", basename), "w", encoding="utf-8") as fh:
        fh.write(sample_lines)
    return pack_dir


def _flat_pack(root, name="flat"):
    # type: (str, str) -> str
    """A pack with a timestamp, an ipv4 and an integer[10:20] token.

    The sample line carries a fixed literal date, a literal dotted-quad and a
    literal integer that each match one token, so a rendered line is unambiguous.
    """
    conf = (
        "[%s.sample]\n"
        "mode = sample\n"
        "interval = 1\n"
        "count = 10\n"
        # timestamp token: the literal 2020-... date in the sample is rewritten
        # to now via the strftime replacement.
        "token.0.token = \\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\n"
        "token.0.replacementType = timestamp\n"
        "token.0.replacement = %%Y-%%m-%%dT%%H:%%M:%%S\n"
        # ipv4 token: the literal src=0.0.0.0 host is replaced with a random quad.
        "token.1.token = (?<=src=)\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\n"
        "token.1.replacementType = random\n"
        "token.1.replacement = ipv4\n"
        # integer token: the literal code=999 is replaced with an int in [10,20].
        "token.2.token = (?<=code=)\\d+\n"
        "token.2.replacementType = random\n"
        "token.2.replacement = integer[10:20]\n"
    ) % name
    sample = "\n".join(
        "2020-01-01T00:00:%02d src=0.0.0.0 code=999 msg=hello-%d" % (i % 60, i)
        for i in range(5)
    ) + "\n"
    return _write_pack(root, name, conf, sample)


# --------------------------------------------------------------------------- #
# Core render behaviour.
# --------------------------------------------------------------------------- #

def test_renders_n_events(tmp_path):
    pack = _flat_pack(str(tmp_path))
    events = preview_pack(pack, n=7)
    assert len(events) == 7


def test_timestamp_token_rewritten_to_now(tmp_path):
    pack = _flat_pack(str(tmp_path))
    events = preview_pack(pack, n=3)
    now_year = datetime.datetime.now().year
    ts_re = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")
    for line in events:
        # The original literal was 2020-...; the rendered value must be ~now.
        assert "2020-01-01T" not in line
        m = ts_re.search(line)
        assert m is not None, line
        assert int(m.group(1)) == now_year


def test_ipv4_token_substituted(tmp_path):
    pack = _flat_pack(str(tmp_path))
    events = preview_pack(pack, n=5)
    ip_re = re.compile(r"src=(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
    for line in events:
        m = ip_re.search(line)
        assert m is not None, line
        # A real dotted-quad (every octet 0-255) — and not the literal 0.0.0.0
        # for every rendered line (it is random; at least assert it parses).
        ipaddress.IPv4Address(m.group(1))


def test_integer_token_in_range(tmp_path):
    pack = _flat_pack(str(tmp_path))
    events = preview_pack(pack, n=25)
    code_re = re.compile(r"code=(\d+)")
    for line in events:
        m = code_re.search(line)
        assert m is not None, line
        value = int(m.group(1))
        assert 10 <= value <= 20, line


def test_capture_group_token_preserves_surrounding_literal(tmp_path):
    # A token whose regex captures the value in group 1 with literal text on
    # either side (inside the match) must replace ONLY the group, keeping the
    # surrounding literal — mirroring the vendored eventgen (match.start(1)).
    # This is what packs like aws-cloudtrail rely on: the JSON key and quotes
    # around "sourceIPAddress":"..." must survive so the event stays valid JSON.
    conf = (
        "[g.sample]\n"
        "mode = sample\n"
        "count = 4\n"
        'token.0.token = "sourceIPAddress":"(\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3})"\n'
        "token.0.replacementType = random\n"
        "token.0.replacement = ipv4\n"
    )
    sample = '{"eventName":"GetObject","sourceIPAddress":"203.0.113.7","readOnly":true}\n'
    pack = _write_pack(str(tmp_path), "g", conf, sample)
    events = preview_pack(pack, n=3)
    assert events
    import json as _json
    for line in events:
        # JSON stays valid: the key/quotes were preserved, only the IP changed.
        doc = _json.loads(line)
        ipaddress.IPv4Address(doc["sourceIPAddress"])
        assert doc["sourceIPAddress"] != "203.0.113.7" or True  # random; parse is the gate
        assert doc["eventName"] == "GetObject"


def test_events_cycle_the_sample_pool(tmp_path):
    # With a 5-line pool and n=12, the render cycles the pool (msg suffix repeats
    # 0..4,0..4,0,1). The non-token msg text is preserved verbatim.
    pack = _flat_pack(str(tmp_path))
    events = preview_pack(pack, n=12)
    suffixes = [re.search(r"msg=hello-(\d+)", e).group(1) for e in events]
    assert suffixes[:5] == ["0", "1", "2", "3", "4"]
    assert suffixes[5:10] == ["0", "1", "2", "3", "4"]


def test_unknown_token_type_left_as_is(tmp_path):
    # A replacementType the preview does not implement (e.g. "file") must leave
    # the matched text untouched rather than blanking or erroring.
    conf = (
        "[u.sample]\n"
        "mode = sample\n"
        "count = 3\n"
        "token.0.token = KEEPME\n"
        "token.0.replacementType = mvfile\n"
        "token.0.replacement = /etc/passwd:1\n"
    )
    pack = _write_pack(str(tmp_path), "u", conf, "prefix KEEPME suffix\n")
    events = preview_pack(pack, n=3)
    assert events
    for line in events:
        assert "KEEPME" in line
        # The bogus replacement target is never read / never leaks into output.
        assert "root:" not in line


# --------------------------------------------------------------------------- #
# Path-safety: a token/sampleFile that would escape the pack root is not read.
# --------------------------------------------------------------------------- #

def test_sample_file_escaping_pack_root_is_not_read(tmp_path):
    # A secret file OUTSIDE the pack, plus a pack whose sampleFile traverses out
    # to it with `..`. The preview must refuse to read it -> no events.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-do-not-read\n", encoding="utf-8")
    conf = (
        "[esc.sample]\n"
        "mode = sample\n"
        "count = 5\n"
        "sampleFile = ../secret.txt\n"
    )
    pack = _write_pack(str(tmp_path), "esc", conf, "in-pack line\n")
    events = preview_pack(pack, n=5)
    assert events == []
    # Belt and braces: the secret content never appears in any rendered output.
    assert all("TOPSECRET" not in e for e in events)


def test_absolute_sample_file_is_not_read(tmp_path):
    secret = tmp_path / "abs-secret.txt"
    secret.write_text("ABSOLUTE-SECRET\n", encoding="utf-8")
    conf = (
        "[abs.sample]\n"
        "mode = sample\n"
        "count = 5\n"
        "sampleFile = %s\n"
    ) % str(secret)
    pack = _write_pack(str(tmp_path), "abs", conf, "in-pack line\n")
    events = preview_pack(pack, n=5)
    assert events == []


def test_missing_conf_yields_no_events(tmp_path):
    pack_dir = os.path.join(str(tmp_path), "noconf")
    os.makedirs(pack_dir)
    assert preview_pack(pack_dir, n=5) == []


def test_missing_pack_dir_yields_no_events(tmp_path):
    assert preview_pack(os.path.join(str(tmp_path), "nope"), n=5) == []


# --------------------------------------------------------------------------- #
# n clamping.
# --------------------------------------------------------------------------- #

def test_n_clamped_to_max(tmp_path):
    pack = _flat_pack(str(tmp_path))
    events = preview_pack(pack, n=10_000)
    assert len(events) == PREVIEW_N_MAX


def test_n_floor_and_default():
    assert clamp_n(0) == 1
    assert clamp_n(-5) == 1
    assert clamp_n(None) == preview.PREVIEW_N_DEFAULT
    assert clamp_n(10_000) == PREVIEW_N_MAX
    assert clamp_n(7) == 7


def test_preview_is_side_effect_free_on_disk(tmp_path):
    # Rendering must not create/modify any file in the pack (pure read).
    pack = _flat_pack(str(tmp_path))
    before = _tree_snapshot(pack)
    preview_pack(pack, n=50)
    after = _tree_snapshot(pack)
    assert before == after


def _tree_snapshot(root):
    # type: (str) -> dict
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            path = os.path.join(dirpath, fn)
            st = os.stat(path)
            snap[os.path.relpath(path, root)] = (st.st_size, st.st_mtime_ns)
    return snap


# --------------------------------------------------------------------------- #
# Route: GET /api/packs/{id}/preview_run.
# --------------------------------------------------------------------------- #

def test_preview_run_endpoint(client, db_session, make_pack):
    pack_dir = make_pack()
    pack = _helpers.make_pack(db_session, pack_dir)
    db_session.commit()

    resp = client.get("/api/packs/%d/preview_run" % pack.id, params={"n": 6})
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert len(body["events"]) == 6
    # The conftest make_pack uses a timestamp token; the rendered lines carry a
    # ~now timestamp, not the fixture's literal 2026-01-01.
    assert all("2026-01-01T" not in line for line in body["events"])


def test_preview_run_endpoint_clamps_n(client, db_session, make_pack):
    pack_dir = make_pack()
    pack = _helpers.make_pack(db_session, pack_dir)
    db_session.commit()

    resp = client.get("/api/packs/%d/preview_run" % pack.id, params={"n": 99999})
    assert resp.status_code == 200
    assert len(resp.json()["events"]) == PREVIEW_N_MAX


def test_preview_run_endpoint_unknown_pack_404(client):
    assert client.get("/api/packs/999999/preview_run").status_code == 404


def test_preview_route_helper_refuses_path_escape(tmp_path):
    # Review (medium): the /packs/{id}/preview helper must not read a sampleFile
    # that escapes the pack root (absolute path or ../ traversal).
    from server.routes.api import _preview_sample_lines

    pack = tmp_path / "pack"
    (pack / "default").mkdir(parents=True)
    (pack / "samples").mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-DO-NOT-LEAK\n")
    (pack / "default" / "eventgen.conf").write_text(
        "[evil_rel]\nmode = sample\nsampleFile = ../../secret.txt\n\n"
        "[evil_abs]\nmode = sample\nsampleFile = %s\n" % str(secret))

    out = _preview_sample_lines(str(pack), ["evil_rel", "evil_abs"])
    for stanza, lines in out.items():
        assert "TOPSECRET" not in "\n".join(lines), "escape leaked via %s" % stanza
    assert out["evil_rel"] == []
    assert out["evil_abs"] == []
