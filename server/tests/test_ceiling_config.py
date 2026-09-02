"""Configurable-ceiling tests over the operator API.

The per-worker ceiling is resolved fleet > env > built-in defaults (see
``engines/ceilings.resolve_ceilings``), and the SAME resolved table must feed
both ``GET /specs/{id}/estimate`` (what the wizard shows) and the submit guard
in ``POST /specs/{id}/run`` (what actually rejects) — an operator must never see
a green estimate and then a ``slice_exceeds_ceiling``. These tests drive both
endpoints through the TestClient with the env layer injected by swapping the
Settings singleton (``dataclasses.replace`` on the conftest settings) and the
fleet layer by editing the seeded fleet row's ``config_json``, mirroring how a
real deployment configures each.
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import select

from server import config as config_mod
from server.models import Fleet

from . import _helpers


def _set_env_ceilings(settings, **kwargs):
    # type: (...) -> None
    """Install a Settings copy with the given ceiling fields (the 'env' layer)."""
    config_mod.set_settings(dataclasses.replace(settings, **kwargs))


def _set_fleet_config(db_session, name, config):
    # type: (...) -> None
    """Write a seeded fleet row's config_json (the 'per-fleet' layer)."""
    fleet = db_session.execute(
        select(Fleet).where(Fleet.name == name)).scalars().first()
    assert fleet is not None, "fleet %r not seeded" % name
    fleet.config_json = config
    db_session.commit()


def _make_spec(client, db_session, settings, make_pack, rate_mode="eps",
               rate_value=20000.0, workers=1):
    # type: (...) -> int
    target = _helpers.make_target(db_session, settings=settings)
    pack = _helpers.make_pack(db_session, make_pack())
    spec = _helpers.make_spec(db_session, pack, target, rate_mode=rate_mode,
                              rate_value=rate_value, workers=workers,
                              fleet="fake-local")
    db_session.commit()
    return spec.id


# --------------------------------------------------------------------------- #
# Defaults: the estimate reports the built-in effective table.
# --------------------------------------------------------------------------- #

def test_estimate_reports_default_effective_ceilings(client, db_session, settings, make_pack, fake_driver):
    spec_id = _make_spec(client, db_session, settings, make_pack,
                         rate_value=1000.0, workers=4)
    est = client.get("/api/specs/%d/estimate" % spec_id)
    assert est.status_code == 200
    body = est.json()
    assert body["ok"] is True
    assert body["ceilings"] == {"max_gb_day_per_worker": 25.0,
                                "max_eps_per_worker": 5000.0}
    assert body["ceiling_limit"] == 5000.0


# --------------------------------------------------------------------------- #
# Env-configured ceiling: raises the guard AND the estimate together.
# --------------------------------------------------------------------------- #

def test_env_ceiling_raises_guard_and_estimate(client, db_session, settings, make_pack, fake_driver):
    # 20000 EPS on 1 worker is 4x over the default; prove the default rejects,
    # then raise the env ceiling and prove BOTH endpoints let it through.
    spec_id = _make_spec(client, db_session, settings, make_pack,
                         rate_value=20000.0, workers=1)

    resp = client.post("/api/specs/%d/run" % spec_id, json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "slice_exceeds_ceiling"
    assert detail["suggested_workers"] == 4

    # Raise BOTH bounds: at 120 B/event, 20000 EPS also implies ~207 GB/day,
    # so the GB/day bound would bind next (whichever binds first wins).
    _set_env_ceilings(settings, max_eps_per_worker=50000.0,
                      max_gb_day_per_worker=250.0)

    est = client.get("/api/specs/%d/estimate" % spec_id)
    body = est.json()
    assert body["ok"] is True
    assert body["ceilings"] == {"max_eps_per_worker": 50000.0,
                                "max_gb_day_per_worker": 250.0}
    assert body["ceiling_limit"] == 50000.0

    resp = client.post("/api/specs/%d/run" % spec_id, json={})
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# Per-fleet override beats the env value (in both directions).
# --------------------------------------------------------------------------- #

def test_fleet_override_beats_env(client, db_session, settings, make_pack, fake_driver):
    # Env raises GB/day to 100, but the spec's fleet clamps it back to 10:
    # a 30 GB/day single-worker run must be rejected against the FLEET's 10
    # (suggested ceil(30/10) = 3), and the estimate must agree.
    _set_env_ceilings(settings, max_gb_day_per_worker=100.0)
    _set_fleet_config(db_session, "fake-local", {"max_gb_day_per_worker": 10})

    spec_id = _make_spec(client, db_session, settings, make_pack,
                         rate_mode="per_day_gb", rate_value=30.0, workers=1)

    est = client.get("/api/specs/%d/estimate" % spec_id)
    body = est.json()
    assert body["ok"] is False
    assert body["ceilings"]["max_gb_day_per_worker"] == 10.0
    assert body["suggested_workers"] == 3

    resp = client.post("/api/specs/%d/run" % spec_id, json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "slice_exceeds_ceiling"
    # Guard and estimate resolved the same table -> the same suggestion.
    assert detail["suggested_workers"] == body["suggested_workers"]
    assert detail["limiting_factor"] == body["limiting_factor"] == "gb_day"


def test_fleet_override_can_raise_over_default(client, db_session, settings, make_pack, fake_driver):
    # No env config at all; the fleet alone lifts both bounds (the derived
    # GB/day of 20000 EPS at 120 B/event is ~207, over the 25 default) and a
    # 20000-EPS single-worker run goes through.
    _set_fleet_config(db_session, "fake-local",
                      {"max_eps_per_worker": 30000,
                       "max_gb_day_per_worker": 250})

    spec_id = _make_spec(client, db_session, settings, make_pack,
                         rate_value=20000.0, workers=1)

    est = client.get("/api/specs/%d/estimate" % spec_id)
    assert est.json()["ok"] is True
    resp = client.post("/api/specs/%d/run" % spec_id, json={})
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# Disabled ceiling: 0 means "no limit", nothing crashes, nothing blocks.
# --------------------------------------------------------------------------- #

def test_disabled_ceiling_is_no_limit(client, db_session, settings, make_pack, fake_driver):
    # The operator who measured 200 GB/day per worker turns the guard off on
    # this fleet; a run far over every default must estimate ok and submit 201.
    _set_fleet_config(db_session, "fake-local",
                      {"max_eps_per_worker": 0, "max_gb_day_per_worker": 0})

    spec_id = _make_spec(client, db_session, settings, make_pack,
                         rate_value=200000.0, workers=1)

    est = client.get("/api/specs/%d/estimate" % spec_id)
    body = est.json()
    assert body["ok"] is True
    assert body["ceiling_limit"] is None
    assert body["ceiling_pct"] is None
    assert body["ceilings"] == {"max_gb_day_per_worker": None,
                                "max_eps_per_worker": None}

    resp = client.post("/api/specs/%d/run" % spec_id, json={})
    assert resp.status_code == 201


def test_env_zero_disables_globally(client, db_session, settings, make_pack, fake_driver):
    _set_env_ceilings(settings, max_eps_per_worker=0.0,
                      max_gb_day_per_worker=0.0)
    spec_id = _make_spec(client, db_session, settings, make_pack,
                         rate_value=500000.0, workers=1)
    assert client.get("/api/specs/%d/estimate" % spec_id).json()["ok"] is True
    assert client.post("/api/specs/%d/run" % spec_id, json={}).status_code == 201


# --------------------------------------------------------------------------- #
# Guard/estimate agreement on the DEFAULT table (no config at all).
# --------------------------------------------------------------------------- #

def test_guard_and_estimate_agree_on_defaults(client, db_session, settings, make_pack, fake_driver):
    spec_id = _make_spec(client, db_session, settings, make_pack,
                         rate_value=20000.0, workers=1)

    est_body = client.get("/api/specs/%d/estimate" % spec_id).json()
    assert est_body["ok"] is False

    resp = client.post("/api/specs/%d/run" % spec_id, json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["suggested_workers"] == est_body["suggested_workers"] == 4
    assert detail["limiting_factor"] == est_body["limiting_factor"] == "eps"


# --------------------------------------------------------------------------- #
# GET /api/fleets reports the effective per-engine ceilings (what the wizard's
# live arithmetic uses), reflecting env + per-fleet layers.
# --------------------------------------------------------------------------- #

def test_fleets_report_effective_ceilings(client, db_session, settings, fake_driver):
    _set_env_ceilings(settings, max_gb_day_per_worker=100.0)
    _set_fleet_config(db_session, "fake-local", {"max_gb_day_per_worker": 200})

    fleets = {f["name"]: f for f in client.get("/api/fleets").json()}

    # The overridden fleet resolves its own value; the others get the env's.
    assert fleets["fake-local"]["ceilings"]["eventgen"] == {
        "max_gb_day_per_worker": 200.0, "max_eps_per_worker": 5000.0}
    assert fleets["swarm-local"]["ceilings"]["eventgen"] == {
        "max_gb_day_per_worker": 100.0, "max_eps_per_worker": 5000.0}
    # Both table engines are reported (the wizard indexes by engine).
    assert set(fleets["fake-local"]["ceilings"]) == {"eventgen", "rawreplay"}
    # The override key itself is on the public config allowlist (plain number,
    # never a credential), so the operator can see WHY the fleet differs.
    assert fleets["fake-local"]["config"]["max_gb_day_per_worker"] == 200
