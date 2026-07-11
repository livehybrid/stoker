"""Smoke tests for the control-plane foundation.

Proves the shared infrastructure imports cleanly and works end-to-end at the
seams the parallel builders depend on: the app boots and ``/healthz`` is 200,
the schema creates, the apportioner sums exactly, ceilings reject over-large
slices, JWTs round-trip and reject tampering, a bundle builds + dedups + its
slice matches what the worker's ``SpecSlice.from_claim`` parses, response bodies
never leak secrets, and the FakeDriver walks the conformance transitions.
"""

from __future__ import annotations

import hashlib

import pytest

from server import crypto
from server.config import Settings
from server.drivers.base import DriverRef, RunSnapshot
from server.engines.apportion import apportion_shares, largest_remainder
from server.engines.ceilings import check_slice


# --------------------------------------------------------------------------- #
# App + schema (the required smoke assertions).
# --------------------------------------------------------------------------- #

def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "stoker-control-plane"


def test_models_create_all(db_engine):
    from sqlalchemy import inspect

    from server import models  # noqa: F401

    tables = set(inspect(db_engine).get_table_names())
    expected = {
        "targets", "packs", "bundles", "specs", "runs", "worker_leases",
        "metric_samples", "run_events", "fleets",
    }
    assert expected.issubset(tables)


def test_fleets_seeded(client, db_session):
    from server.models import Fleet

    names = {f.name for f in db_session.query(Fleet).all()}
    assert {"fake-local", "swarm-local"}.issubset(names)


# --------------------------------------------------------------------------- #
# Apportionment (sums exactly).
# --------------------------------------------------------------------------- #

def test_largest_remainder_sums_exactly():
    for total in (0, 1, 7, 100, 1543, 99999):
        for n in (1, 2, 3, 4, 7):
            parts = largest_remainder(total, [1.0] * n)
            assert sum(parts) == total
            assert len(parts) == n
            assert all(p >= 0 for p in parts)


def test_apportion_eps_sums_exactly():
    shares = apportion_shares("eps", 1000.0, 3)
    assert len(shares) == 3
    assert all(set(s.keys()) == {"eps"} for s in shares)
    assert sum(s["eps"] for s in shares) == pytest.approx(1000.0)


def test_apportion_count_interval_integer_split():
    shares = apportion_shares("count_interval", 100, 3)
    counts = [int(s["count"]) for s in shares]
    assert sum(counts) == 100
    assert set().union(*[set(s.keys()) for s in shares]) == {"count"}


# --------------------------------------------------------------------------- #
# Ceilings.
# --------------------------------------------------------------------------- #

def test_ceiling_ok_under_limit():
    result = check_slice("eps", 1000.0, bytes_per_event=120)
    assert result.ok


def test_ceiling_rejects_over_eps():
    result = check_slice("eps", 20000.0, bytes_per_event=120)
    assert not result.ok
    assert result.suggested_workers is not None and result.suggested_workers >= 2
    assert result.limiting_factor in ("eps", "gb_day")


def test_ceiling_count_interval_always_ok():
    assert check_slice("count_interval", None).ok


# --------------------------------------------------------------------------- #
# JWT round-trip + tamper reject.
# --------------------------------------------------------------------------- #

def test_jwt_round_trip(settings):
    kid = crypto.new_kid()
    token = crypto.mint_run_jwt(812, kid, settings=settings)
    claims = crypto.verify_run_jwt(token, 812, settings=settings)
    assert str(claims["run_id"]) == "812"
    assert claims["kid"] == kid


def test_jwt_wrong_run_rejected(settings):
    token = crypto.mint_run_jwt(812, crypto.new_kid(), settings=settings)
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt(token, 999, settings=settings)


def test_jwt_tamper_rejected(settings):
    token = crypto.mint_run_jwt(812, crypto.new_kid(), settings=settings)
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt(tampered, 812, settings=settings)


def test_jwt_foreign_key_rejected(settings):
    token = crypto.mint_run_jwt(812, crypto.new_kid(), settings=settings)
    other = Settings(
        database_url=settings.database_url,
        master_key=crypto.generate_master_key(),
        jwt_ttl_s=3600, public_base_url="http://x", worker_image="x",
        portainer_host=None, portainer_token=None, portainer_endpoint=6,
        bundle_dir=settings.bundle_dir, dogfood_hec_url=None,
        dogfood_hec_token=None, port=8080,
    )
    with pytest.raises(crypto.JWTError):
        crypto.verify_run_jwt(token, 812, settings=other)


# --------------------------------------------------------------------------- #
# Fernet secret round-trip.
# --------------------------------------------------------------------------- #

def test_fernet_round_trip(settings):
    secret = "hec-token-abc123"
    ct = crypto.encrypt(secret, settings=settings)
    assert secret not in ct
    assert crypto.decrypt(ct, settings=settings) == secret


# --------------------------------------------------------------------------- #
# Bundle build + dedup + worker-parseable slice.
# --------------------------------------------------------------------------- #

def test_bundle_build_and_dedup(settings, make_pack):
    from server.bundles import build_from_pack

    pack_dir = make_pack()
    first = build_from_pack(pack_dir, bundle_dir=settings.bundle_dir)
    assert len(first.digest) == 64
    assert not first.reused
    # Rebuild identical pack -> same digest, reused.
    second = build_from_pack(pack_dir, bundle_dir=settings.bundle_dir)
    assert second.digest == first.digest
    assert second.reused

    # The stored file's sha256 matches the digest (content-addressed).
    with open(first.path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == first.digest


def test_bundle_unpacks_to_worker_pack_root(settings, make_pack):
    """The tarball must unpack to a dir the worker's _find_pack_root accepts."""
    import tarfile
    import tempfile

    from server.bundles import build_from_pack

    pack_dir = make_pack()
    built = build_from_pack(pack_dir, bundle_dir=settings.bundle_dir)
    with tempfile.TemporaryDirectory() as dest:
        with tarfile.open(built.path, "r:*") as tar:
            tar.extractall(dest, filter="data")
        # One level down: <pack>/default/eventgen.conf.
        import os

        found = False
        for entry in os.listdir(dest):
            candidate = os.path.join(dest, entry, "default", "eventgen.conf")
            if os.path.isfile(candidate):
                found = True
        assert found, "bundle did not unpack to <pack>/default/eventgen.conf"


def test_slice_parses_with_worker_model(settings, make_pack, db_session):
    """A slice built by lifecycle.build_slice must satisfy SpecSlice.from_claim."""
    stoker_agent = pytest.importorskip("stoker_agent.slice")
    from server import lifecycle
    from server.bundles import build_from_pack
    from server.models import Bundle, Run, Spec, Target, WorkerLease

    # Minimal target + spec + run + bundle + lease, enough to build a slice.
    target = Target(name="t1", hec_url="http://192.168.0.222:8088",
                    default_index="loadtest", verify_tls=False, env_tag="lab")
    db_session.add(target)
    db_session.flush()
    spec = Spec(name="s1", pack_id=1, target_id=target.id, engine="eventgen",
                rate_mode="eps", rate_value=1000.0, workers=4,
                overrides_json={"host": "apigw-{slot}"}, fleet="fake-local")
    db_session.add(spec)
    db_session.flush()

    built = build_from_pack(make_pack(), bundle_dir=settings.bundle_dir)
    bundle = Bundle(pack_id=1, digest=built.digest, size_bytes=built.size_bytes,
                    path=built.path)
    db_session.add(bundle)
    db_session.flush()

    run = Run(spec_id=spec.id, state="provisioning", bundle_id=bundle.id,
              jwt_kid=crypto.new_kid(),
              spec_snapshot_json=lifecycle.build_spec_snapshot(spec, target))
    db_session.add(run)
    db_session.flush()

    lease = WorkerLease(run_id=run.id, slot=2, share_json={"eps": 250.0},
                        lease_id="le_test", state="claimed")
    db_session.add(lease)
    db_session.flush()

    slice_doc = lifecycle.build_slice(run, lease, settings=settings)
    parsed = stoker_agent.SpecSlice.from_claim(slice_doc)
    assert parsed.slot == 2
    assert parsed.total_workers == 4
    assert parsed.rate_mode == "eps"
    assert parsed.rate_value == 250.0
    assert parsed.bundle_sha256 == built.digest
    assert parsed.overrides.get("host") == "apigw-2"  # {slot} substituted
    assert parsed.hec_url == "http://192.168.0.222:8088"


# --------------------------------------------------------------------------- #
# No secret material in any response body.
# --------------------------------------------------------------------------- #

def test_target_out_has_no_token_field():
    from server.schemas import TargetOut

    assert "token" not in TargetOut.model_fields
    assert "token_encrypted" not in TargetOut.model_fields


def test_slice_schema_has_no_hec_token():
    from server.schemas import BundleRef, HecSlice, SpecSliceOut, TelemetrySlice

    # No field named token anywhere in the slice or its nested objects.
    for model in (SpecSliceOut, HecSlice, BundleRef, TelemetrySlice):
        assert not any("token" in name for name in model.model_fields), model.__name__
    # And a serialised slice instance carries no "token" key.
    dumped = SpecSliceOut(
        run_id=1, slot=0, total_workers=1, lease_id="le_x", engine="eventgen",
        bundle=BundleRef(url="http://x/api/agent/bundles/d.tgz", sha256="d"),
        share={"eps": 1.0}, hec=HecSlice(url="http://h:8088", index="i"),
    ).model_dump()
    assert not any("token" in k for k in dumped)
    assert not any("token" in k for k in dumped["hec"])


# --------------------------------------------------------------------------- #
# Agent auth: a bad/absent bearer is rejected before any body handling.
# --------------------------------------------------------------------------- #

def test_agent_claim_requires_bearer(client):
    resp = client.post("/api/agent/runs/1/claim",
                       json={"holder": "h", "protocol_version": 1})
    assert resp.status_code == 401


def test_agent_claim_wrong_run_rejected(client, settings):
    token = crypto.mint_run_jwt(999, crypto.new_kid(), settings=settings)
    resp = client.post(
        "/api/agent/runs/1/claim",
        json={"holder": "h", "protocol_version": 1},
        headers={"Authorization": "Bearer %s" % token},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# FakeDriver conformance (create -> status -> scale -> stop -> destroy).
# --------------------------------------------------------------------------- #

def test_fake_driver_conformance():
    from server.drivers.fake import FakeDriver

    driver = FakeDriver()
    snap = RunSnapshot(run_id=1, image="img", env={}, labels={"stoker.run": "1"},
                       driver_opts={})
    ref = driver.create(snap, 4)
    assert isinstance(ref, DriverRef)
    assert driver.status(ref).desired == 4
    assert driver.status(ref).running == 4  # bookkeeping mode reports reached

    driver.scale(ref, 6)
    assert driver.status(ref).desired == 6

    driver.stop(ref, grace_s=45)
    assert driver.status(ref).desired == 0  # stopped fleet reports 0 desired

    driver.destroy(ref)
    assert driver.is_destroyed(ref)
    # Idempotent destroy.
    driver.destroy(ref)
    assert driver.status(ref).desired == 0


def test_driver_ref_json_round_trip():
    ref = DriverRef(kind="fake", id="abc", raw={"run_id": 1})
    again = DriverRef.from_json(ref.to_json())
    assert again == ref
    assert DriverRef.from_json(None) is None


# --------------------------------------------------------------------------- #
# Lifecycle stubs raise NotImplementedError (contract for the Core builder).
# --------------------------------------------------------------------------- #

def test_lifecycle_domain_functions_are_stubbed():
    from server import lifecycle

    for name in ("provision_run", "claim_lease", "mark_ready",
                 "record_heartbeat", "record_final", "supervisor_tick",
                 "reconcile_on_boot", "stop_run", "scale_run", "rescale_run"):
        assert hasattr(lifecycle, name), "missing lifecycle.%s" % name


def test_lifecycle_helpers_are_implemented():
    """Pure helpers the builders share must be callable, not stubs."""
    from server import lifecycle

    assert lifecycle.share_for_mode("eps", 5) == {"eps": 5.0}
    assert lifecycle.resolve_overrides({"host": "h-{slot}"}, 3) == {"host": "h-3"}
    assert lifecycle.new_lease_id().startswith("le_")
    assert lifecycle.cmd_continue() == {"command": "continue"}
    assert lifecycle.cmd_superseded() == {"command": "superseded"}
    counters = lifecycle.counters_from_payload(
        {"events_total": 10, "eps": 5.0, "garbage": "x", "bytes_total": "nope"})
    assert counters["events_total"] == 10
    assert counters["eps"] == 5.0
    assert counters["bytes_total"] is None  # non-numeric coerced to None
