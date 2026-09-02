"""Fleet seeding, name resolution and the fleets API.

Covers the in-cluster (EKS-pod) deployment path at the control-plane level:

* ``seed_fleets`` seeds ``k8s-local`` (driver ``k8s``, namespace from the
  ``K8S_NAMESPACE`` setting) alongside ``fake-local`` / ``swarm-local``;
* ``lifecycle.resolve_fleet_driver`` resolves a spec's fleet NAME through the
  ``fleets`` table. This is the launch-path regression: the name used to be
  handed to the driver factory as a *driver* name, so any named fleet
  ("swarm-local", "k8s-local", "eks") failed with "unknown fleet driver ...
  expected one of: swarm, k8s, fake" on a host where boot had not pre-cached
  it (e.g. an EKS pod with no Portainer configured);
* ``POST /specs/{id}/run`` launches on a fleet registered only as a DB row,
  and rejects an unknown fleet with a 422 ``unknown_fleet`` body (never a 500);
* ``GET /api/fleets`` lists registered fleets with a REDACTED config: only the
  addressing allowlist is exposed. The EKS fleet design stores (encrypted)
  credentials in ``config_json``; even ciphertext must never appear in a GET
  body.
"""

from __future__ import annotations

import dataclasses
import os

import pytest
from sqlalchemy import select

from server import drivers as drivers_mod
from server import lifecycle
from server.drivers.base import DriverError
from server.drivers.fake import FakeDriver
from server.models import Fleet, Run

from . import _helpers


# --------------------------------------------------------------------------- #
# seed_fleets
# --------------------------------------------------------------------------- #

def test_seed_fleets_seeds_k8s_local(db_session, settings):
    lifecycle.seed_fleets(db_session, settings=settings)
    fleets = {f.name: f for f in db_session.execute(select(Fleet)).scalars().all()}
    assert {"fake-local", "swarm-local", "k8s-local"} <= set(fleets)
    k8s = fleets["k8s-local"]
    assert k8s.driver == "k8s"
    assert (k8s.config_json or {}).get("namespace") == settings.k8s_namespace


def test_seed_fleets_namespace_follows_settings(db_session, settings):
    custom = dataclasses.replace(settings, k8s_namespace="loadtest-ns")
    lifecycle.seed_fleets(db_session, settings=custom)
    k8s = db_session.execute(
        select(Fleet).where(Fleet.name == "k8s-local")).scalars().first()
    assert (k8s.config_json or {}).get("namespace") == "loadtest-ns"


def test_seed_fleets_node_selector_follows_settings(db_session, settings):
    custom = dataclasses.replace(
        settings, k8s_node_selector=(("workload", "stoker"),))
    lifecycle.seed_fleets(db_session, settings=custom)
    k8s = db_session.execute(
        select(Fleet).where(Fleet.name == "k8s-local")).scalars().first()
    assert (k8s.config_json or {}).get("node_selector") == {"workload": "stoker"}


def test_seed_fleets_no_node_selector_key_when_unset(db_session, settings):
    lifecycle.seed_fleets(db_session, settings=settings)
    k8s = db_session.execute(
        select(Fleet).where(Fleet.name == "k8s-local")).scalars().first()
    # Absent, not present-and-empty: the driver treats a missing key as "no
    # fleet default" and the config stays byte-identical to pre-feature seeds.
    assert "node_selector" not in (k8s.config_json or {})


def test_parse_node_selector_env():
    from server.config import ConfigError, _parse_node_selector

    assert _parse_node_selector({}) == ()
    assert _parse_node_selector(
        {"K8S_NODE_SELECTOR": "workload=stoker"}) == (("workload", "stoker"),)
    assert _parse_node_selector(
        {"K8S_NODE_SELECTOR": " workload = stoker , pool = spot "}
    ) == (("workload", "stoker"), ("pool", "spot"))
    with pytest.raises(ConfigError):
        _parse_node_selector({"K8S_NODE_SELECTOR": "workload"})
    with pytest.raises(ConfigError):
        _parse_node_selector({"K8S_NODE_SELECTOR": "=stoker"})


def test_list_fleets_shows_node_selector(client, db_session):
    # node_selector is addressing, not a credential: it must survive the
    # config redaction allowlist so operators can see fleet placement.
    db_session.add(Fleet(name="eks-pinned", driver="k8s", config_json={
        "namespace": "stoker",
        "node_selector": {"workload": "stoker"},
    }))
    db_session.commit()

    resp = client.get("/api/fleets")
    assert resp.status_code == 200
    fleet = next(f for f in resp.json() if f["name"] == "eks-pinned")
    assert (fleet["config"] or {}).get("node_selector") == {"workload": "stoker"}


def test_seed_fleets_is_idempotent_and_preserves_edits(db_session, settings):
    lifecycle.seed_fleets(db_session, settings=settings)
    k8s = db_session.execute(
        select(Fleet).where(Fleet.name == "k8s-local")).scalars().first()
    k8s.config_json = {"namespace": "operator-edited"}
    db_session.commit()

    lifecycle.seed_fleets(db_session, settings=settings)
    rows = db_session.execute(select(Fleet)).scalars().all()
    assert len([f for f in rows if f.name == "k8s-local"]) == 1
    k8s = db_session.execute(
        select(Fleet).where(Fleet.name == "k8s-local")).scalars().first()
    assert k8s.config_json == {"namespace": "operator-edited"}


# --------------------------------------------------------------------------- #
# resolve_fleet_driver: row name > cache-registered name > bare driver name.
# --------------------------------------------------------------------------- #

def test_resolve_fleet_driver_by_row_name(db_session, fake_driver):
    db_session.add(Fleet(name="row-fake", driver="fake", config_json={}))
    db_session.commit()
    driver = lifecycle.resolve_fleet_driver(db_session, "row-fake")
    assert isinstance(driver, FakeDriver)


def test_resolve_fleet_driver_bare_driver_name(db_session, fake_driver):
    driver = lifecycle.resolve_fleet_driver(db_session, "fake")
    assert isinstance(driver, FakeDriver)


def test_resolve_fleet_driver_cache_registered_name(db_session, fake_driver):
    sentinel = FakeDriver()
    drivers_mod.register_driver("bound-name", sentinel)
    assert lifecycle.resolve_fleet_driver(db_session, "bound-name") is sentinel


def test_resolve_fleet_driver_unknown_name_lists_registered(db_session, settings,
                                                            fake_driver):
    lifecycle.seed_fleets(db_session, settings=settings)
    with pytest.raises(DriverError) as excinfo:
        lifecycle.resolve_fleet_driver(db_session, "typo-fleet")
    msg = str(excinfo.value)
    assert "typo-fleet" in msg
    assert "not a registered fleet" in msg
    assert "swarm-local" in msg and "k8s-local" in msg


# --------------------------------------------------------------------------- #
# Launch path: named fleets resolve through the fleets table.
# --------------------------------------------------------------------------- #

def test_launch_on_fleet_registered_only_as_db_row(client, db_session, settings,
                                                   make_pack, fake_driver):
    # A fleet that exists ONLY as a fleets row (nothing pre-registered in the
    # driver cache under this name) must launch: the launch path has to read
    # the row and build the row's driver, not treat the name as a driver name.
    db_session.add(Fleet(name="row-only-fleet", driver="fake", config_json={}))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, rate_mode="eps",
                              rate_value=500.0, workers=2, fleet="row-only-fleet")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 201, resp.text
    run = db_session.get(Run, resp.json()["run_id"])
    assert run is not None


def test_launch_unknown_fleet_is_422_unknown_fleet(client, db_session, settings,
                                                   make_pack, fake_driver):
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, rate_mode="eps",
                              rate_value=500.0, workers=1, fleet="no-such-fleet")
    db_session.commit()

    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "unknown_fleet"
    assert detail["fleet"] == "no-such-fleet"
    assert "registered" in detail["detail"]
    # Resolution fails before provisioning: no run row is created.
    assert db_session.execute(select(Run)).scalars().first() is None


# --------------------------------------------------------------------------- #
# GET /api/fleets: the seeded fleets, with a redacted config.
# --------------------------------------------------------------------------- #

def test_list_fleets_returns_seeded_fleets(client):
    resp = client.get("/api/fleets")
    assert resp.status_code == 200
    by_name = {f["name"]: f for f in resp.json()}
    assert {"fake-local", "swarm-local", "k8s-local"} <= set(by_name)
    assert by_name["k8s-local"]["driver"] == "k8s"
    assert by_name["k8s-local"]["config"]["namespace"]


def test_list_fleets_redacts_config_to_the_allowlist(client, db_session):
    ciphertext = "gAAAAABfake-ciphertext-must-not-leak"
    db_session.add(Fleet(name="eks", driver="k8s", config_json={
        "namespace": "stoker",
        "kube_context": "eks-eu-west-2",
        "aws_access_key_id_encrypted": ciphertext,
        "aws_secret_access_key_encrypted": ciphertext,
    }))
    db_session.commit()

    resp = client.get("/api/fleets")
    assert resp.status_code == 200
    eks = next(f for f in resp.json() if f["name"] == "eks")
    assert eks["config"] == {"namespace": "stoker", "kube_context": "eks-eu-west-2"}
    assert ciphertext not in resp.text
    assert "encrypted" not in resp.text


# --------------------------------------------------------------------------- #
# In-process fleet: seeding flag + the submit gates.
# --------------------------------------------------------------------------- #

def test_seed_fleets_inprocess_only_when_enabled(db_session, settings):
    lifecycle.seed_fleets(db_session, settings=settings)
    names = {f.name for f in db_session.execute(select(Fleet)).scalars().all()}
    assert "inprocess-local" not in names  # default: opt-in only

    enabled = dataclasses.replace(settings, inprocess_fleet_enabled=True)
    lifecycle.seed_fleets(db_session, settings=enabled)
    row = db_session.execute(
        select(Fleet).where(Fleet.name == "inprocess-local")).scalars().first()
    assert row is not None
    assert row.driver == "inprocess"


def test_inprocess_settings_parse():
    from server.config import load_settings

    parsed = load_settings(env={"STOKER_INPROCESS_FLEET": "1",
                                "STOKER_INPROCESS_MAX_WORKERS": "0"})
    assert parsed.inprocess_fleet_enabled is True
    assert parsed.inprocess_max_workers == 1  # clamped to >= 1
    assert load_settings(env={}).inprocess_fleet_enabled is False


def _inprocess_spec(db_session, settings, make_pack, fleet_name, workers=1,
                    pack_dir=None):
    """A spec on an inprocess-driver fleet row, plus its target/pack rows."""
    db_session.add(Fleet(name=fleet_name, driver="inprocess", config_json={}))
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, pack_dir or make_pack())
    spec = _helpers.make_spec(db_session, pack, target, rate_mode="eps",
                              rate_value=100.0, workers=workers,
                              fleet=fleet_name)
    db_session.commit()
    return spec


def test_launch_inprocess_over_cap_is_422(client, db_session, settings,
                                          make_pack, fake_driver):
    spec = _inprocess_spec(db_session, settings, make_pack, "inproc-cap",
                           workers=3)  # default cap is 2
    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "inprocess_worker_cap"
    assert detail["max_workers"] == 2


def test_launch_inprocess_custom_code_denied(client, db_session, settings,
                                             make_pack, fake_driver):
    pack_dir = make_pack(name="binpack")
    bin_dir = os.path.join(pack_dir, "bin")
    os.makedirs(bin_dir)
    with open(os.path.join(bin_dir, "evil.py"), "w", encoding="utf-8") as fh:
        fh.write("print('custom code')\n")

    spec = _inprocess_spec(db_session, settings, make_pack, "inproc-code",
                           workers=1, pack_dir=pack_dir)
    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "inprocess_custom_code_denied"
    assert any("bin/" in e for e in detail["errors"])


def test_launch_inprocess_disabled_is_fleet_unavailable(client, db_session,
                                                        settings, make_pack,
                                                        fake_driver):
    # Gates pass (1 worker, clean pack) but the driver refuses to build while
    # STOKER_INPROCESS_FLEET is off -> a clean 422, distinguished from a typo'd
    # fleet name.
    spec = _inprocess_spec(db_session, settings, make_pack, "inproc-off",
                           workers=1)
    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "fleet_unavailable"
    assert "STOKER_INPROCESS_FLEET" in detail["detail"]


def test_launch_inprocess_within_gates_provisions(client, db_session, settings,
                                                  make_pack, fake_driver):
    # With the gates satisfied the launch proceeds. The driver cache is primed
    # with a bookkeeping FakeDriver under the fleet's name so the test provisions
    # without spawning real agent subprocesses (the e2e test owns that path);
    # the submit gates still ran against the row's driver kind ("inprocess").
    drivers_mod.register_driver("inproc-ok", FakeDriver())
    spec = _inprocess_spec(db_session, settings, make_pack, "inproc-ok",
                           workers=2)
    resp = client.post("/api/specs/%d/run" % spec.id, json={})
    assert resp.status_code == 201, resp.text
    assert db_session.get(Run, resp.json()["run_id"]) is not None
