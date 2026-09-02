"""Multi-pack specs: several eventgen packs merged into ONE run bundle.

The design under test (``server.bundles.build_from_packs`` + the spec/submit
wiring): a spec keeps its primary ``pack_id`` and may add ``extra_pack_ids``;
at provision time the control plane merges the selected packs into a single
synthesised bundle — namespaced stanzas and a flat namespaced ``samples/`` —
so the worker contract does not change at all. These tests pin:

* the namespacing (two packs with COLLIDING stanza + sample names merge
  without collision, and ``file``-token paths under ``samples/`` are rewritten
  to the renamed files);
* determinism (the same pack set yields the same digest regardless of
  selection order);
* the worker's own conf-rewrite splitting a share across the merged stanza
  union proportionally to their declared counts (rate apportionment);
* every refusal (rawreplay pack, metrics pack, non-eventgen engine, replay
  stanza, single pack) with the established ``error`` slug + ``detail`` body;
* that a single-pack spec still produces byte-for-byte its old bundle;
* the end-to-end spec -> run path on the fake fleet: snapshot, leases, claim
  slice and the agent bundle download all serve the merged bundle.
"""

from __future__ import annotations

import io
import json
import os
import tarfile

import pytest

from server import bundles, lifecycle
from server.bundles import BundleError, build_from_pack, build_from_packs
from server.models import Bundle, Pack, Run, Spec

from . import _helpers


# --------------------------------------------------------------------------- #
# Local pack factories (colliding names on purpose — conftest's make_pack
# derives the stanza name from the pack name, which defeats the collision).
# --------------------------------------------------------------------------- #

def _write_pack(root, dirname, stanza="web.sample", count=100, name=None,
                file_token=False, line="GET /index.html 200 hello-world-line",
                pack_yaml_extra=""):
    # type: (...) -> str
    """A tiny eventgen pack whose stanza/sample names the caller controls."""
    pack_dir = root / dirname
    (pack_dir / "default").mkdir(parents=True)
    (pack_dir / "samples").mkdir()
    conf = (
        "[%s]\n"
        "mode = sample\n"
        "interval = 1\n"
        "count = %d\n"
        "earliest = -1s\n"
        "latest = now\n"
    ) % (stanza, count)
    if file_token:
        conf += (
            "token.0.token = (\\d{3})\n"
            "token.0.replacementType = file\n"
            "token.0.replacement = samples/codes.sample\n"
        )
        (pack_dir / "samples" / "codes.sample").write_text(
            "200\n404\n500\n", encoding="utf-8")
    (pack_dir / "default" / "eventgen.conf").write_text(conf, encoding="utf-8")
    (pack_dir / "samples" / stanza).write_text(
        ("%s\n" % line) * 10, encoding="utf-8")
    (pack_dir / "pack.yaml").write_text(
        "name: %s\nengine: eventgen\n%s" % (name or dirname, pack_yaml_extra),
        encoding="utf-8")
    return str(pack_dir)


def _write_rawreplay_pack(root, dirname="replaypack"):
    # type: (...) -> str
    """The minimal rawreplay pack the merge/spec guards must refuse."""
    pack_dir = root / dirname
    pack_dir.mkdir(parents=True)
    (pack_dir / "capture.log").write_text("one\ntwo\n", encoding="utf-8")
    (pack_dir / "pack.yaml").write_text(
        "name: %s\nengine: rawreplay\nreplay:\n  dataset: capture.log\n"
        "  mode: rate\n" % dirname, encoding="utf-8")
    return str(pack_dir)


def _tar_members(path):
    # type: (str) -> dict
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


# --------------------------------------------------------------------------- #
# The merge itself (bundles layer).
# --------------------------------------------------------------------------- #

def test_colliding_stanzas_and_samples_merge_namespaced(tmp_path, settings):
    """Two packs both shipping a ``web.sample`` stanza + file merge with every
    stanza, sample file and file-token path renamed under its pack namespace,
    so nothing collides and every reference stays internally consistent."""
    a = _write_pack(tmp_path, "packa", count=100, file_token=True)
    b = _write_pack(tmp_path, "packb", count=300)
    ns = bundles.merge_pack_namespaces([("packa", 1), ("packb", 2)])
    built = build_from_packs(list(zip(ns, [a, b])), bundle_dir=settings.bundle_dir)

    members = _tar_members(built.path)
    assert "mergedpack/samples/packa--web.sample" in members
    assert "mergedpack/samples/packb--web.sample" in members
    assert "mergedpack/samples/packa--codes.sample" in members
    conf = members["mergedpack/default/eventgen.conf"].decode("utf-8")
    assert "[packa--web.sample]" in conf
    assert "[packb--web.sample]" in conf
    # The file token now points at the renamed sample inside the merged pack.
    assert "samples/packa--codes.sample" in conf
    assert "samples/codes.sample\n" not in conf

    manifest = json.loads(members["mergedpack/stoker.json"])
    assert manifest["engine"] == "eventgen"
    assert manifest["stanzas"] == ["packa--web.sample", "packb--web.sample"]
    assert [p["namespace"] for p in manifest["merged_packs"]] == ["packa", "packb"]
    assert manifest["estimates"]["bytes_per_event"] > 0


def test_merged_digest_is_independent_of_selection_order(tmp_path, settings):
    a = _write_pack(tmp_path, "packa", count=100)
    b = _write_pack(tmp_path, "packb", count=300)
    ns = bundles.merge_pack_namespaces([("packa", 1), ("packb", 2)])
    one = build_from_packs([(ns[0], a), (ns[1], b)], bundle_dir=settings.bundle_dir)
    two = build_from_packs([(ns[1], b), (ns[0], a)], bundle_dir=settings.bundle_dir)
    assert one.digest == two.digest
    assert two.reused  # content-addressed dedup, not a rebuild


def test_merge_namespaces_disambiguate_a_name_clash():
    """Two packs sanitising to the same namespace get their id appended; a
    clean set keeps plain sanitised names (stable digests for the common case)."""
    assert bundles.merge_pack_namespaces([("web access", 1), ("apigw", 2)]) == \
        ["web-access", "apigw"]
    assert bundles.merge_pack_namespaces([("web", 1), ("web", 2)]) == \
        ["web-1", "web-2"]


def test_merge_refusals(tmp_path, settings):
    """rawreplay and single-pack merges are BundleErrors at the bundles layer
    (the route refuses earlier with a 422; this is the defence in depth)."""
    a = _write_pack(tmp_path, "packa")
    raw = _write_rawreplay_pack(tmp_path)
    with pytest.raises(BundleError, match="rawreplay"):
        build_from_packs([("packa", a), ("replaypack", raw)],
                         bundle_dir=settings.bundle_dir)
    with pytest.raises(BundleError, match="at least two"):
        build_from_packs([("packa", a)], bundle_dir=settings.bundle_dir)


def test_merge_refuses_a_replay_stanza(tmp_path, settings):
    a = _write_pack(tmp_path, "packa")
    r = _write_pack(tmp_path, "packr")
    conf_path = os.path.join(r, "default", "eventgen.conf")
    with open(conf_path, "r", encoding="utf-8") as fh:
        conf = fh.read()
    with open(conf_path, "w", encoding="utf-8") as fh:
        fh.write(conf.replace("mode = sample", "mode = replay"))
    with pytest.raises(BundleError, match="replay"):
        build_from_packs([("packa", a), ("packr", r)],
                         bundle_dir=settings.bundle_dir)


def test_per_pack_globals_do_not_leak_across_packs(tmp_path, settings):
    """One pack's [global] keys are materialised into ITS stanzas only."""
    a = _write_pack(tmp_path, "packa")
    conf_path = os.path.join(a, "default", "eventgen.conf")
    with open(conf_path, "r", encoding="utf-8") as fh:
        conf = fh.read()
    with open(conf_path, "w", encoding="utf-8") as fh:
        fh.write("[global]\nrandomizeCount = 0.5\n\n" + conf)
    b = _write_pack(tmp_path, "packb")
    built = build_from_packs([("packa", a), ("packb", b)],
                             bundle_dir=settings.bundle_dir)
    conf = _tar_members(built.path)[
        "mergedpack/default/eventgen.conf"].decode("utf-8")
    sections = {}
    current = None
    for line in conf.splitlines():
        if line.startswith("["):
            current = line.strip("[]")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    assert "global" not in sections  # materialised, then dropped
    assert any("randomizeCount" in l for l in sections["packa--web.sample"])
    assert not any("randomizeCount" in l for l in sections["packb--web.sample"])


def test_merged_bytes_per_event_is_weight_weighted(tmp_path):
    """The merged estimate is the declared-eps-weighted mean of the per-stanza
    estimates — the expected bytes/event of the merged stream."""
    # 100 eps of 10-byte lines + 300 eps of 30-byte lines -> 25 bytes/event.
    a = _write_pack(tmp_path, "packa", count=100, line="a" * 10)
    b = _write_pack(tmp_path, "packb", count=300, line="b" * 30)
    est = bundles.merged_est_bytes_per_event([a, b])
    assert est == pytest.approx((100 * 10 + 300 * 30) / 400.0, abs=0.5)


def test_worker_rewrite_apportions_across_the_merged_stanzas(tmp_path, settings):
    """Rate apportionment: the WORKER's own conf-rewrite splits an eps share
    across the merged stanza union by declared count/interval, so a pack
    declaring 3x the events/s receives ~3x the run rate. This runs the real
    ``stoker_agent.confrewrite`` against the merged conf."""
    confrewrite = pytest.importorskip("stoker_agent.confrewrite")

    a = _write_pack(tmp_path, "packa", count=100)
    b = _write_pack(tmp_path, "packb", count=300)
    built = build_from_packs([("packa", a), ("packb", b)],
                             bundle_dir=settings.bundle_dir)
    members = _tar_members(built.path)
    merged_dir = tmp_path / "unpacked"
    with tarfile.open(built.path) as tar:
        tar.extractall(str(merged_dir))
    conf_path = str(merged_dir / "mergedpack" / "default" / "eventgen.conf")

    parser = confrewrite.load_conf(conf_path)
    confrewrite.rewrite(parser, "eps", 400.0, 1.0,
                        str(merged_dir / "mergedpack" / "samples"))
    counts = {s: int(parser.get(s, "count"))
              for s in confrewrite.sample_sections(parser)}
    assert counts == {"packa--web.sample": 100, "packb--web.sample": 300}
    assert members  # (namespaced samples exist for the stanzas above)


# --------------------------------------------------------------------------- #
# Spec create/update validation.
# --------------------------------------------------------------------------- #

def _spec_body(pack_id, target_id, extra_ids=None, **overrides):
    # type: (...) -> dict
    body = {
        "name": "multi", "pack_id": pack_id, "target_id": target_id,
        "engine": "eventgen", "rate_mode": "eps", "rate_value": 100.0,
        "workers": 2, "fleet": "fake-local",
    }
    if extra_ids is not None:
        body["extra_pack_ids"] = extra_ids
    body.update(overrides)
    return body


def test_create_spec_normalises_extra_pack_ids(client, db_session, settings,
                                               tmp_path):
    target = _helpers.make_target(db_session, settings=settings)
    a = _helpers.make_pack(db_session, _write_pack(tmp_path, "packa"), name="packa")
    b = _helpers.make_pack(db_session, _write_pack(tmp_path, "packb"), name="packb")
    db_session.commit()

    # Duplicates and the primary itself are dropped from the extra list.
    resp = client.post("/api/specs", json=_spec_body(
        a.id, target.id, extra_ids=[b.id, b.id, a.id]))
    assert resp.status_code == 201
    assert resp.json()["extra_pack_ids_json"] == [b.id]

    # An unknown extra id is a 422, exactly like an unknown pack_id.
    resp = client.post("/api/specs", json=_spec_body(
        a.id, target.id, extra_ids=[999999]))
    assert resp.status_code == 422


def test_create_spec_refuses_unmergeable_extras(client, db_session, settings,
                                                tmp_path):
    target = _helpers.make_target(db_session, settings=settings)
    a = _helpers.make_pack(db_session, _write_pack(tmp_path, "packa"), name="packa")
    raw = _helpers.make_pack(
        db_session, _write_rawreplay_pack(tmp_path), name="replaypack")
    metrics = _helpers.make_pack(db_session, "builder://metrics", name="metricpack")
    metrics.builder_config_json = {
        "resolution_s": 10,
        "metrics": [{"name": "m", "kind": "gauge", "min": 0, "p95": 1, "max": 2}]}
    db_session.commit()

    for bad in (raw.id, metrics.id):
        resp = client.post("/api/specs", json=_spec_body(
            a.id, target.id, extra_ids=[bad]))
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"] == "multi_pack_engine_unsupported"
        assert "detail" in detail

    # A non-eventgen spec engine cannot carry extra packs either.
    b = _helpers.make_pack(db_session, _write_pack(tmp_path, "packb"), name="packb")
    db_session.commit()
    resp = client.post("/api/specs", json=_spec_body(
        a.id, target.id, extra_ids=[b.id], engine="rawreplay"))
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "multi_pack_engine_unsupported"


def test_update_spec_sets_and_clears_extras(client, db_session, settings, tmp_path):
    target = _helpers.make_target(db_session, settings=settings)
    a = _helpers.make_pack(db_session, _write_pack(tmp_path, "packa"), name="packa")
    b = _helpers.make_pack(db_session, _write_pack(tmp_path, "packb"), name="packb")
    spec = _helpers.make_spec(db_session, a, target, fleet="fake-local")
    db_session.commit()

    resp = client.put("/api/specs/%d" % spec.id, json={"extra_pack_ids": [b.id]})
    assert resp.status_code == 200
    assert resp.json()["extra_pack_ids_json"] == [b.id]

    # Repointing the primary onto a listed extra drops it from the extras.
    resp = client.put("/api/specs/%d" % spec.id, json={"pack_id": b.id})
    assert resp.status_code == 200
    assert resp.json()["extra_pack_ids_json"] is None

    # And [] clears the set explicitly.
    client.put("/api/specs/%d" % spec.id,
               json={"pack_id": a.id, "extra_pack_ids": [b.id]})
    resp = client.put("/api/specs/%d" % spec.id, json={"extra_pack_ids": []})
    assert resp.status_code == 200
    assert resp.json()["extra_pack_ids_json"] is None


def test_delete_pack_referenced_as_extra_is_refused(client, db_session, settings,
                                                    tmp_path):
    target = _helpers.make_target(db_session, settings=settings)
    a = _helpers.make_pack(db_session, _write_pack(tmp_path, "packa"), name="packa")
    b = _helpers.make_pack(db_session, _write_pack(tmp_path, "packb"), name="packb")
    spec = _helpers.make_spec(db_session, a, target, fleet="fake-local")
    spec.extra_pack_ids_json = [b.id]
    db_session.commit()

    resp = client.delete("/api/packs/%d" % b.id)
    assert resp.status_code == 409
    assert db_session.get(Pack, b.id) is not None


# --------------------------------------------------------------------------- #
# Submit gates + end-to-end on the fake fleet.
# --------------------------------------------------------------------------- #

def test_submit_refuses_when_an_extra_turns_rawreplay(client, db_session,
                                                      settings, tmp_path,
                                                      fake_driver):
    """The submit-time re-check: a pack that becomes rawreplay AFTER the spec
    was saved (e.g. a repo resync) is refused at launch, not merged blind."""
    target = _helpers.make_target(db_session, settings=settings)
    a = _helpers.make_pack(db_session, _write_pack(tmp_path, "packa"), name="packa")
    b_dir = _write_pack(tmp_path, "packb")
    b = _helpers.make_pack(db_session, b_dir, name="packb")
    spec = _helpers.make_spec(db_session, a, target, fleet="fake-local")
    spec.extra_pack_ids_json = [b.id]
    db_session.commit()

    with open(os.path.join(b_dir, "pack.yaml"), "w", encoding="utf-8") as fh:
        fh.write("name: packb\nengine: rawreplay\n"
                 "replay:\n  dataset: samples/web.sample\n")
    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "multi_pack_engine_unsupported"
    assert db_session.query(Run).count() == 0


def test_submit_lint_gate_covers_extra_packs(client, db_session, settings,
                                             tmp_path, fake_driver):
    target = _helpers.make_target(db_session, settings=settings)
    a = _helpers.make_pack(db_session, _write_pack(tmp_path, "packa"), name="packa")
    b_dir = _write_pack(tmp_path, "packb")
    b = _helpers.make_pack(db_session, b_dir, name="packb")
    spec = _helpers.make_spec(db_session, a, target, fleet="fake-local")
    spec.extra_pack_ids_json = [b.id]
    db_session.commit()

    os.remove(os.path.join(b_dir, "samples", "web.sample"))  # break the extra
    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "pack_lint_failed"
    assert any("packb" in e for e in detail["errors"])


def test_two_pack_spec_runs_end_to_end_on_the_fake_fleet(
        client, db_session, settings, tmp_path, fake_driver):
    """The whole path: create a two-pack spec via the API, launch it, and
    verify the run's bundle is the merged one — snapshot, leases, the claim
    slice and the agent bundle download all agree."""
    target = _helpers.make_target(db_session, settings=settings)
    a_dir = _write_pack(tmp_path, "packa", count=100)
    b_dir = _write_pack(tmp_path, "packb", count=300)
    a = _helpers.make_pack(db_session, a_dir, name="packa")
    b = _helpers.make_pack(db_session, b_dir, name="packb")
    db_session.commit()

    resp = client.post("/api/specs", json=_spec_body(
        a.id, target.id, extra_ids=[b.id]))
    assert resp.status_code == 201
    spec_id = resp.json()["id"]

    resp = client.post("/api/specs/%d/run" % spec_id, json={})
    assert resp.status_code == 201, resp.json()
    run = db_session.get(Run, resp.json()["run_id"])
    assert run.state == lifecycle.STATE_PROVISIONING
    assert run.spec_snapshot_json["extra_pack_ids"] == [b.id]
    assert len(_helpers.leases_by_slot(db_session, run)) == 2

    # The run's digest is exactly the deterministic merged digest.
    ns = bundles.merge_pack_namespaces([("packa", a.id), ("packb", b.id)])
    expected = build_from_packs(list(zip(ns, [a_dir, b_dir])),
                                bundle_dir=settings.bundle_dir)
    assert run.resolved_sha == expected.digest
    bundle = db_session.get(Bundle, run.bundle_id)
    assert bundle.digest == expected.digest
    assert bundle.pack_id == a.id  # the primary keeps the row's provenance

    # The claim slice hands the worker the merged bundle by digest.
    lease = lifecycle.claim_lease(db_session, run, holder="w-0")
    sl = lifecycle.build_slice(run, lease, settings=settings)
    assert sl["engine"] == "eventgen"
    assert sl["bundle"]["sha256"] == expected.digest
    db_session.commit()

    # ...and the agent bundle endpoint serves it under the run JWT.
    resp = client.get("/api/agent/bundles/%s.tgz" % expected.digest,
                      headers=_helpers.auth_header(run, settings))
    assert resp.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(resp.content)) as tar:
        names = tar.getnames()
    assert "mergedpack/samples/packa--web.sample" in names
    assert "mergedpack/samples/packb--web.sample" in names


def test_single_pack_spec_builds_its_old_bundle_unchanged(
        client, db_session, settings, tmp_path, fake_driver):
    """No extras -> the classic single-pack build path, byte-for-byte."""
    target = _helpers.make_target(db_session, settings=settings)
    a_dir = _write_pack(tmp_path, "packa")
    a = _helpers.make_pack(db_session, a_dir, name="packa")
    db_session.commit()

    resp = client.post("/api/specs", json=_spec_body(a.id, target.id))
    assert resp.status_code == 201
    assert resp.json()["extra_pack_ids_json"] is None
    resp = client.post("/api/specs/%d/run" % resp.json()["id"], json={})
    assert resp.status_code == 201
    run = db_session.get(Run, resp.json()["run_id"])
    assert run.resolved_sha == build_from_pack(
        a_dir, bundle_dir=settings.bundle_dir).digest
    assert "extra_pack_ids" not in (run.spec_snapshot_json or {})
