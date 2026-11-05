"""The specs list carries each spec's run count and latest run.

So a batch of specs (one per day of backfill, say) can be checked at a glance
for which have run and how the latest run of each ended.
"""

from __future__ import annotations

from server import lifecycle
from server.models import Run, utcnow

from . import _helpers


def _run(db, spec, state):
    run = Run(spec_id=spec.id, state=state, created_at=utcnow())
    if state in lifecycle.TERMINAL_STATES:
        run.ended_at = utcnow()
        run.end_reason = "duration"
    db.add(run)
    db.flush()
    return run


def test_list_reports_run_count_and_latest_run(client, db_session, settings, make_pack):
    pack = _helpers.make_pack(db_session, make_pack())
    target = _helpers.make_target(db_session, settings=settings)
    never = _helpers.make_spec(db_session, pack, target, name="never")
    twice = _helpers.make_spec(db_session, pack, target, name="twice")
    _run(db_session, twice, lifecycle.STATE_FAILED)
    latest = _run(db_session, twice, lifecycle.STATE_COMPLETED)
    db_session.commit()

    resp = client.get("/api/specs")
    assert resp.status_code == 200, resp.text
    by_name = {s["name"]: s for s in resp.json()}

    assert by_name["never"]["run_count"] == 0
    assert by_name["never"]["last_run"] is None

    assert by_name["twice"]["run_count"] == 2
    last = by_name["twice"]["last_run"]
    assert last["id"] == latest.id
    assert last["state"] == lifecycle.STATE_COMPLETED
    assert last["ended_at"] is not None
    assert last["end_reason"] == "duration"


def test_single_spec_route_carries_the_same_summary(client, db_session, settings, make_pack):
    pack = _helpers.make_pack(db_session, make_pack())
    target = _helpers.make_target(db_session, settings=settings)
    spec = _helpers.make_spec(db_session, pack, target)
    run = _run(db_session, spec, lifecycle.STATE_FAILED)
    db_session.commit()
    body = client.get("/api/specs/%d" % spec.id).json()
    assert body["run_count"] == 1
    assert body["last_run"]["id"] == run.id
    assert body["last_run"]["state"] == lifecycle.STATE_FAILED
