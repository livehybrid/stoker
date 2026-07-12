"""Boot reconciliation: the stray-fleet sweep in ``lifecycle.reconcile_on_boot``.

Phase 1 (DB-driven adopt/fail) is exercised elsewhere (``test_lease_state`` /
``test_review_fixes``). This module covers **phase 2**: destroying labelled
workloads a driver still owns that have *no live DB run* — the orphan fleet a
control-plane crash can leave blasting a target with no supervisor — while never
touching a workload that maps to a live run.

The shared ``FakeDriver`` (the ``fake_driver`` fixture, registered for both the
``fake-local`` and ``swarm-local`` fleet names) is the estate: ``full_run``
launches the *live* run's fleet through it, and the tests then plant a *stray*
fleet on the same driver via a direct ``create`` for a run id with no live row.
``reconcile_on_boot`` is then driven with that driver as the only fleet.
"""

from __future__ import annotations

import pytest

from server import lifecycle
from server.drivers.base import DriverError, DriverRef, RunSnapshot

from . import _helpers

pytestmark = pytest.mark.usefixtures("fake_driver")


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #

def _stray_snapshot(run_id):
    # type: (int) -> RunSnapshot
    """A minimal RunSnapshot for planting a stray fleet directly on a driver."""
    return RunSnapshot(
        run_id=run_id,
        image="ghcr.io/livehybrid/stoker-worker:test",
        env={"STOKER_RUN_ID": str(run_id)},
        labels={"stoker.run": str(run_id)},
        driver_opts={},
        stop_grace_s=45,
    )


def _plant_stray(driver, run_id, workers=2):
    # type: (object, int, int) -> DriverRef
    """Create a labelled fleet on ``driver`` with no corresponding live DB run."""
    return driver.create(_stray_snapshot(run_id), workers)


# --------------------------------------------------------------------------- #
# The core behaviour: stray destroyed, live run's fleet left alone.
# --------------------------------------------------------------------------- #

def test_stray_fleet_destroyed_live_run_left_alone(
        db_session, make_pack, settings, fake_driver):
    """A labelled workload with no DB run is destroyed; a live run's is adopted."""
    pack_dir = make_pack()
    # A live (provisioning) run — its fleet is created on fake_driver by full_run.
    live = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                             workers=2)["run"]
    live_ref = lifecycle.driver_ref_of(live)

    # Plant a stray: a fleet the driver owns for a run id that has no DB row at
    # all (the snapshot that would have created it was lost in a crash).
    stray_run_id = live.id + 5000
    stray_ref = _plant_stray(fake_driver, stray_run_id, workers=3)

    # Precondition: the driver owns both fleets.
    owned_before = fake_driver.list_run_ids()
    assert live.id in owned_before
    assert stray_run_id in owned_before

    lifecycle.reconcile_on_boot(db_session, {"fake-local": fake_driver})
    db_session.commit()

    # The stray is destroyed; the live run's fleet is untouched.
    assert fake_driver.is_destroyed(stray_ref)
    assert not fake_driver.is_destroyed(live_ref)
    owned_after = fake_driver.list_run_ids()
    assert stray_run_id not in owned_after
    assert live.id in owned_after

    # The live run was adopted (not failed), and stays non-terminal.
    db_session.refresh(live)
    assert live.state not in lifecycle.TERMINAL_STATES


def test_stray_for_terminal_run_is_destroyed(
        db_session, make_pack, settings, fake_driver):
    """A workload whose DB run is terminal (completed/failed) counts as a stray."""
    pack_dir = make_pack()
    ctx = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                            workers=1)
    run = ctx["run"]
    ref = lifecycle.driver_ref_of(run)
    # Drive the run terminal but leave its (now orphaned) fleet up on the driver.
    lifecycle.transition_run(db_session, run, lifecycle.STATE_COMPLETED,
                             end_reason="completed")
    db_session.commit()
    assert run.id in fake_driver.list_run_ids()  # fleet still up (leak)

    lifecycle.reconcile_on_boot(db_session, {"fake-local": fake_driver})
    db_session.commit()

    # Terminal run -> not in the live set -> its lingering fleet is swept.
    assert fake_driver.is_destroyed(ref)
    assert run.id not in fake_driver.list_run_ids()


def test_stray_sweep_writes_audit_event_when_run_row_exists(
        db_session, make_pack, settings, fake_driver):
    """Destroying a stray whose (terminal) run row exists records an audit event."""
    from server.models import RunEvent
    from sqlalchemy import select

    pack_dir = make_pack()
    run = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                            workers=1)["run"]
    lifecycle.transition_run(db_session, run, lifecycle.STATE_FAILED,
                             end_reason="orphaned")
    db_session.commit()

    lifecycle.reconcile_on_boot(db_session, {"fake-local": fake_driver})
    db_session.commit()

    kinds = [
        e.kind for e in db_session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id)).scalars().all()
    ]
    assert "stray_destroyed" in kinds


# --------------------------------------------------------------------------- #
# Safety: never destroy the estate on an empty/errored enumeration.
# --------------------------------------------------------------------------- #

def test_empty_enumeration_destroys_nothing(
        db_session, make_pack, settings, fake_driver):
    """A driver that owns a live run's fleet and nothing stray destroys nothing."""
    pack_dir = make_pack()
    live = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                             workers=2)["run"]
    live_ref = lifecycle.driver_ref_of(live)

    lifecycle.reconcile_on_boot(db_session, {"fake-local": fake_driver})
    db_session.commit()

    assert not fake_driver.is_destroyed(live_ref)
    assert live.id in fake_driver.list_run_ids()


def test_enumeration_error_skips_sweep_never_destroys_all(
        db_session, make_pack, settings, fake_driver, monkeypatch):
    """If list_run_ids raises, the sweep is skipped — never 'all are strays'."""
    pack_dir = make_pack()
    # Two live runs, both with fleets up on the driver.
    a = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                          workers=1)["run"]
    ref_a = lifecycle.driver_ref_of(a)
    # Plant a genuine stray too — it must ALSO survive, because a failed
    # enumeration means we cannot safely tell strays from live and skip entirely.
    stray_ref = _plant_stray(fake_driver, a.id + 9000, workers=1)

    def _boom():
        raise DriverError("enumeration backend down")

    monkeypatch.setattr(fake_driver, "list_run_ids", _boom)

    lifecycle.reconcile_on_boot(db_session, {"fake-local": fake_driver})
    db_session.commit()

    # Nothing was destroyed: the sweep bailed on the enumeration error.
    assert not fake_driver.is_destroyed(ref_a)
    assert not fake_driver.is_destroyed(stray_ref)


def test_non_enumerable_driver_is_skipped(
        db_session, make_pack, settings, fake_driver, monkeypatch):
    """A driver whose list_run_ids raises NotImplementedError is skipped cleanly."""
    pack_dir = make_pack()
    live = _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver,
                             workers=1)["run"]
    live_ref = lifecycle.driver_ref_of(live)

    def _unsupported():
        raise NotImplementedError("this driver cannot enumerate")

    monkeypatch.setattr(fake_driver, "list_run_ids", _unsupported)

    # Must not raise, and must destroy nothing.
    lifecycle.reconcile_on_boot(db_session, {"fake-local": fake_driver})
    db_session.commit()
    assert not fake_driver.is_destroyed(live_ref)


def test_sweep_deduplicates_shared_driver_across_fleet_names(
        db_session, make_pack, settings, fake_driver):
    """One driver bound to several fleet names is enumerated once, not per name."""
    calls = {"n": 0}
    real = fake_driver.list_run_ids

    def _counting():
        calls["n"] += 1
        return real()

    fake_driver.list_run_ids = _counting  # type: ignore[assignment]

    pack_dir = make_pack()
    _helpers.full_run(db_session, pack_dir, settings, driver=fake_driver, workers=1)

    # The conftest fixture registers the SAME instance for both names; passing
    # both must still enumerate the shared estate a single time.
    lifecycle.reconcile_on_boot(
        db_session, {"fake-local": fake_driver, "swarm-local": fake_driver})
    db_session.commit()
    assert calls["n"] == 1
