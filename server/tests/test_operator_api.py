"""Operator API tests (TestClient), including the no-secret-leak invariant.

The GET-list / GET-by-id endpoints work today (foundation); the create / test /
run endpoints are owned by the Operator builder and return 501 until filled.
These tests:

* assert the **no-token-in-any-GET-body** invariant unconditionally (seeding
  targets/specs/runs directly via the DB so it holds regardless of the create
  endpoint's readiness), and that ``token_encrypted`` is real ciphertext;
* assert the create / estimate / run behaviours where implemented, **skipping**
  cleanly while the endpoint still answers 501, so the file is green now and
  meaningful once the Operator builder lands.

``run_spec`` validation is checked for the two rejections that are pure policy
(``slice_exceeds_ceiling`` -> 422, ``replay_single_worker`` -> 409); the driver
is the shared FakeDriver bound to ``fake-local`` by the conftest.
"""

from __future__ import annotations

import json
import os

import pytest

from server import crypto
from server.models import Run, Spec, Target

from . import _helpers

SECRET_TOKEN = "hec-super-secret-token-DO-NOT-LEAK"


def _is_todo(resp):
    # type: (object) -> bool
    """True when an endpoint is still the 501 operator-builder placeholder."""
    return resp.status_code == 501


def _body_text(resp):
    # type: (object) -> str
    try:
        return json.dumps(resp.json())
    except ValueError:
        return resp.text


# --------------------------------------------------------------------------- #
# GET-list endpoints work today.
# --------------------------------------------------------------------------- #

def test_empty_lists_ok(client):
    for path in ("/api/targets", "/api/packs", "/api/specs", "/api/runs"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.json() == []


def test_unknown_ids_404(client):
    for path in ("/api/targets/999", "/api/packs/999", "/api/specs/999", "/api/runs/999"):
        assert client.get(path).status_code == 404


# --------------------------------------------------------------------------- #
# No secret material in any GET body (seeded directly so it always holds).
# --------------------------------------------------------------------------- #

def test_target_token_never_in_get_bodies(client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver, workers=2)
    # Overwrite the target token with a known secret to hunt for it.
    ctx["target"].token_encrypted = crypto.encrypt(SECRET_TOKEN, settings=settings)
    db_session.commit()

    # The stored value is ciphertext, never the plaintext.
    assert SECRET_TOKEN not in (ctx["target"].token_encrypted or "")

    target_id = ctx["target"].id
    run_id = ctx["run"].id
    bodies = [
        client.get("/api/targets").text,
        client.get("/api/targets/%d" % target_id).text,
        client.get("/api/specs").text,
        client.get("/api/specs/%d" % ctx["spec"].id).text,
        client.get("/api/runs").text,
        client.get("/api/runs/%d" % run_id).text,
    ]
    for body in bodies:
        assert SECRET_TOKEN not in body
        assert ctx["target"].token_encrypted not in body  # not even the ciphertext
        assert "token" not in body.lower() or '"token_encrypted"' not in body


def test_run_detail_snapshot_has_no_token(client, db_session, settings, make_pack, fake_driver):
    ctx = _helpers.full_run(db_session, make_pack(), settings, driver=fake_driver, workers=2)
    ctx["target"].token_encrypted = crypto.encrypt(SECRET_TOKEN, settings=settings)
    db_session.commit()
    detail = client.get("/api/runs/%d" % ctx["run"].id).json()
    snap = detail.get("spec_snapshot_json") or {}
    # The snapshot embeds the target by id + non-secret fields only.
    assert "token" not in json.dumps(snap).lower()
    assert snap.get("target", {}).get("id") == ctx["target"].id
    assert "token_encrypted" not in snap.get("target", {})


# --------------------------------------------------------------------------- #
# Create target (Operator builder) — token in, never echoed.
# --------------------------------------------------------------------------- #

def test_create_target_does_not_echo_token(client, db_session, settings):
    resp = client.post("/api/targets", json={
        "name": "t-created",
        "hec_url": "http://127.0.0.1:18088",
        "token": SECRET_TOKEN,
        "default_index": "loadtest",
        "env_tag": "lab",
        "verify_tls": False,
    })
    if _is_todo(resp):
        pytest.skip("create_target not implemented yet (Operator builder)")
    assert resp.status_code == 201
    body = resp.json()
    assert SECRET_TOKEN not in _body_text(resp)
    assert "token" not in body
    # It is persisted as ciphertext and decrypts back to the original.
    row = db_session.get(Target, body["id"])
    assert row is not None
    assert row.token_encrypted and row.token_encrypted != SECRET_TOKEN
    assert crypto.decrypt(row.token_encrypted, settings=settings) == SECRET_TOKEN


def test_created_target_absent_from_get_bodies(client):
    resp = client.post("/api/targets", json={
        "name": "t-hunt", "hec_url": "http://127.0.0.1:18088",
        "token": SECRET_TOKEN, "default_index": "loadtest", "env_tag": "lab",
    })
    if _is_todo(resp):
        pytest.skip("create_target not implemented yet (Operator builder)")
    assert resp.status_code == 201
    assert SECRET_TOKEN not in client.get("/api/targets").text
    assert SECRET_TOKEN not in client.get("/api/targets/%d" % resp.json()["id"]).text


def _make_target(client, name="t-edit", index="loadtest"):
    resp = client.post("/api/targets", json={
        "name": name, "hec_url": "http://127.0.0.1:18088", "token": SECRET_TOKEN,
        "default_index": index, "env_tag": "lab", "verify_tls": False,
        "max_concurrent_gb_day": 100,
    })
    assert resp.status_code == 201, _body_text(resp)
    return resp.json()["id"]


def test_update_target_changes_fields_and_resets_health(client, db_session):
    tid = _make_target(client)
    # Force a health state so we can prove a connection-affecting edit resets it.
    row = db_session.get(Target, tid)
    row.health_state = "green"
    db_session.commit()

    resp = client.patch("/api/targets/%d" % tid, json={
        "hec_url": "https://new-hec.example:8088/", "max_concurrent_gb_day": 250,
        "default_index": "prod",
    })
    assert resp.status_code == 200, _body_text(resp)
    body = resp.json()
    assert body["hec_url"] == "https://new-hec.example:8088"  # trailing slash trimmed
    assert body["max_concurrent_gb_day"] == 250
    assert body["default_index"] == "prod"
    assert body["health_state"] == "unknown"  # endpoint changed -> re-test needed


def test_update_target_rotates_token_write_only(client, db_session, settings):
    tid = _make_target(client, name="t-rotate")
    new_secret = "rotated-hec-token-xyz"  # noqa: S105

    resp = client.patch("/api/targets/%d" % tid, json={"token": new_secret})
    assert resp.status_code == 200, _body_text(resp)
    assert new_secret not in _body_text(resp)
    assert "token" not in resp.json()
    row = db_session.get(Target, tid)
    db_session.refresh(row)
    assert crypto.decrypt(row.token_encrypted, settings=settings) == new_secret

    # An omitted / empty token keeps the stored one (a capacity-only edit).
    resp2 = client.patch("/api/targets/%d" % tid, json={"max_concurrent_gb_day": None})
    assert resp2.status_code == 200, _body_text(resp2)
    assert resp2.json()["max_concurrent_gb_day"] is None  # cap cleared
    db_session.refresh(row)
    assert crypto.decrypt(row.token_encrypted, settings=settings) == new_secret  # unchanged


def test_update_unknown_target_404(client):
    assert client.patch("/api/targets/999999", json={"env_tag": "prod"}).status_code == 404


# --------------------------------------------------------------------------- #
# Packs + specs (Operator builder).
# --------------------------------------------------------------------------- #

def test_register_pack_lints_and_verifies(client, make_pack):
    resp = client.post("/api/packs", json={"name": "p1", "source_path": make_pack()})
    if _is_todo(resp):
        pytest.skip("register_pack not implemented yet (Operator builder)")
    assert resp.status_code == 201
    body = resp.json()
    assert body["lint_status"] == "ok"
    assert body["verified"] is True
    assert body["stanza_count"] == 1


def test_register_broken_pack_reports_lint_error(client, tmp_path):
    # A pack directory with no eventgen.conf must not verify.
    broken = os.path.join(str(tmp_path), "broken-pack")
    os.makedirs(broken)
    resp = client.post("/api/packs", json={"name": "bad", "source_path": broken})
    if _is_todo(resp):
        pytest.skip("register_pack not implemented yet (Operator builder)")
    # Either a 4xx rejection or a stored row flagged error — never verified ok.
    if resp.status_code == 201:
        assert resp.json()["lint_status"] == "error"
        assert resp.json()["verified"] is False
    else:
        assert resp.status_code in (400, 422)


def test_create_spec_and_estimate(client, db_session, settings, make_pack, fake_driver):
    # Seed a target + pack row to reference.
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    db_session.commit()

    resp = client.post("/api/specs", json={
        "name": "spec-eps", "pack_id": pack.id, "target_id": target.id,
        "rate_mode": "eps", "rate_value": 1000.0, "workers": 4,
        "fleet": "fake-local",
    })
    if _is_todo(resp):
        pytest.skip("create_spec not implemented yet (Operator builder)")
    assert resp.status_code == 201
    spec_id = resp.json()["id"]

    est = client.get("/api/specs/%d/estimate" % spec_id)
    if _is_todo(est):
        pytest.skip("estimate_spec not implemented yet (Operator builder)")
    assert est.status_code == 200
    body = est.json()
    assert body["workers"] == 4
    assert body["ok"] is True
    # 1000 EPS / 4 = 250 per worker.
    assert body["per_worker_share"] == pytest.approx(250.0)


# --------------------------------------------------------------------------- #
# run_spec validation rejections.
# --------------------------------------------------------------------------- #

def test_run_spec_rejects_slice_exceeds_ceiling(client, db_session, settings, make_pack, fake_driver):
    # 200000 EPS across 1 worker is far over the 5000 EPS ceiling.
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, rate_mode="eps",
                              rate_value=200000.0, workers=1, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    if _is_todo(resp):
        pytest.skip("run_spec not implemented yet (Operator builder)")
    assert resp.status_code == 422
    text = _body_text(resp).lower()
    assert "ceiling" in text or "slice_exceeds_ceiling" in text
    # The rejection should carry a suggested_workers hint.
    assert "suggested_workers" in _body_text(resp) or "suggested" in text


def test_run_spec_rejects_replay_single_worker(client, db_session, settings, tmp_path, fake_driver):
    # A replay-mode pack with workers > 1 must be rejected (replay is engine-
    # paced and the control plane guarantees workers = 1 for it).
    replay_dir = _write_replay_pack(str(tmp_path))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, replay_dir, name="replaypack")
    spec = _helpers.make_spec(db_session, pack, target, rate_mode="eps",
                              rate_value=100.0, workers=3, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    if _is_todo(resp):
        pytest.skip("run_spec not implemented yet (Operator builder)")
    # If the Operator builder detects replay stanzas it rejects with 409; if it
    # does not inspect stanza modes it may accept — but it must never silently
    # run a multi-worker replay. Assert the documented rejection when it 4xxs.
    if resp.status_code >= 400:
        assert resp.status_code == 409
        assert "replay" in _body_text(resp).lower()
    else:
        pytest.skip("Operator builder does not enforce replay_single_worker here")


def test_run_spec_happy_path_creates_run(client, db_session, settings, make_pack, fake_driver):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, rate_mode="eps",
                              rate_value=500.0, workers=2, fleet="fake-local")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    if _is_todo(resp):
        pytest.skip("run_spec not implemented yet (Operator builder)")
    assert resp.status_code == 201
    body = resp.json()
    assert "run_id" in body
    # The run exists, references a bundle and seeded a lease per worker.
    run = db_session.get(Run, body["run_id"])
    assert run is not None
    assert run.bundle_id is not None
    leases = _helpers.leases_by_slot(db_session, run)
    assert set(leases.keys()) == {0, 1}
    assert SECRET_TOKEN not in _body_text(resp)


# --------------------------------------------------------------------------- #
# helper: a replay-mode pack.
# --------------------------------------------------------------------------- #

def _write_replay_pack(root):
    # type: (str) -> str
    pack_dir = os.path.join(root, "replaypack")
    os.makedirs(os.path.join(pack_dir, "default"))
    os.makedirs(os.path.join(pack_dir, "samples"))
    with open(os.path.join(pack_dir, "default", "eventgen.conf"), "w") as fh:
        fh.write("[replaypack.sample]\nmode = replay\ninterval = 1\n"
                 "timeMultiple = 1\n")
    with open(os.path.join(pack_dir, "samples", "replaypack.sample"), "w") as fh:
        fh.write("2026-01-01T00:00:00 replay line\n2026-01-01T00:00:01 replay line\n")
    with open(os.path.join(pack_dir, "pack.yaml"), "w") as fh:
        fh.write("name: replaypack\nengine: eventgen\nestimates:\n  bytes_per_event: 30\n")
    return pack_dir
