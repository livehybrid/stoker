"""How far a bounded run has got, and when it should finish.

A backfill knows how many events it has to write (its window times its
density), so its progress is events done against events planned, and its
finish is the events left divided by the rate the fleet is *actually*
achieving. The delivery cap is only what was asked for: twelve workers aimed at
16,666 eps each and holding 15,000 finish about a tenth later than the cap
says, and the estimate has to show that.

A live run with a duration ends on the clock (start + duration + time spent
paused), whatever its throughput. An unbounded run has no end to estimate.

The burn-down is built from the workers' heartbeat counters, which are
cumulative per slot. A slot reclaimed by a restarted worker counts from zero
again; that drop is carried as an offset rather than read as events undone.
"""

import collections
import datetime
import math
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import lifecycle
from .models import MetricSample, Run

# The actual rate is the fleet's throughput over this trailing window: long
# enough to smooth heartbeat jitter, short enough to follow a rescale.
RATE_WINDOW_S = 300.0
# A running fleet whose newest heartbeat is older than this has no current
# rate to project from.
STALE_AFTER_S = 120.0
DEFAULT_POINTS = 120
MIN_BUCKET_S = 5.0

_ACTIVE = {lifecycle.STATE_RELEASING, lifecycle.STATE_RUNNING, lifecycle.STATE_DRAINING}


def _epoch(ts):
    # type: (datetime.datetime) -> float
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts.timestamp()


def _from_epoch(s):
    # type: (Optional[float]) -> Optional[datetime.datetime]
    if s is None:
        return None
    return datetime.datetime.fromtimestamp(s, tz=datetime.timezone.utc)


def planned_events(snap):
    # type: (Dict[str, Any]) -> Optional[int]
    """How many events a backfill run was planned to write, if it says.

    Runs launched since the plan was recorded carry it (``backfill_plan``);
    older eventgen backfills are worked out from their window and density.
    An older metrics backfill cannot be (its series count is not in the
    snapshot), so it has no planned total.
    """
    plan = snap.get("backfill_plan") or {}
    events = plan.get("events")
    if isinstance(events, (int, float)) and events > 0:
        return int(events)
    bf = snap.get("backfill")
    if not bf or snap.get("engine") == "metrics":
        return None
    try:
        window = float(bf["end_s"]) - float(bf["start_s"])
        density = float(bf.get("density_eps") or snap.get("rate_value") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if window <= 0 or density <= 0:
        return None
    return int(math.ceil(window * density))


def _scan(db, run_id, points, now):
    # type: (Session, int, int, float) -> Tuple[List[Tuple[float, int]], int, Optional[float], Optional[float]]
    """One pass over the run's heartbeats.

    Returns the fleet's cumulative events as a down-sampled series of
    ``(epoch_s, events)``, the latest fleet total, the actual rate over the
    trailing window, and the newest heartbeat's time.
    """
    rows = db.execute(
        select(MetricSample.ts, MetricSample.slot, MetricSample.events_total)
        .where(MetricSample.run_id == run_id, MetricSample.events_total.isnot(None))
        .order_by(MetricSample.ts, MetricSample.slot)
    ).all()
    if not rows:
        return [], 0, None, None
    first = _epoch(rows[0][0])
    newest = _epoch(rows[-1][0])
    bucket = max(MIN_BUCKET_S, (newest - first) / max(1, points))

    last = {}  # type: Dict[int, int]
    offset = {}  # type: Dict[int, int]
    current = {}  # type: Dict[int, int]
    total = 0
    series = []  # type: List[Tuple[float, int]]
    tail = collections.deque()  # type: collections.deque
    for ts, slot, events in rows:
        t = _epoch(ts)
        events = int(events)
        prev = last.get(slot)
        if prev is not None and events < prev:
            offset[slot] = offset.get(slot, 0) + prev
        last[slot] = events
        value = offset.get(slot, 0) + events
        total += value - current.get(slot, 0)
        current[slot] = value
        # One point per bucket: the last total seen in it.
        if series and int((t - first) // bucket) == int((series[-1][0] - first) // bucket):
            series[-1] = (t, total)
        else:
            series.append((t, total))
        # The trailing window keeps one point at or before its start.
        tail.append((t, total))
        while len(tail) > 2 and tail[1][0] <= newest - RATE_WINDOW_S:
            tail.popleft()

    rate = None
    t0, v0 = tail[0]
    span = newest - t0
    if span > 0:
        rate = max(0.0, (total - v0) / span)
    return series, total, rate, newest


def run_progress(db, run, points=DEFAULT_POINTS, now=None):
    # type: (Session, Run, int, Optional[float]) -> Dict[str, Any]
    now = float(now) if now is not None else lifecycle.utcnow().timestamp()
    snap = run.spec_snapshot_json or {}
    started = run.t0 or run.created_at
    started_s = _epoch(started) if started is not None else None
    terminal = run.state in lifecycle.TERMINAL_STATES
    active = run.state in _ACTIVE
    target_eps = None
    if snap.get("rate_mode") == "eps" and isinstance(snap.get("rate_value"), (int, float)):
        target_eps = float(snap["rate_value"])

    out = {
        "run_id": run.id,
        "state": run.state,
        "kind": "unbounded",
        "started_at": started,
        "target_eps": target_eps,
        "rate_window_s": RATE_WINDOW_S,
        "planned_events": None,
        "done_events": None,
        "remaining_events": None,
        "fraction": None,
        "recent_eps": None,
        "stale": False,
        "eta_s": None,
        "expected_end": None,
        "target_end": None,
        "series": [],
    }  # type: Dict[str, Any]

    planned = planned_events(snap) if snap.get("backfill") else None
    if planned is None:
        duration = snap.get("duration_s")
        if not duration or started_s is None:
            return out
        paused = float(run.paused_s or 0)
        if run.paused_at is not None:
            paused += max(0.0, now - _epoch(run.paused_at))
        end = started_s + float(duration) + paused
        elapsed = min(float(duration), max(0.0, now - started_s - paused))
        out.update(kind="duration",
                   fraction=1.0 if terminal else min(1.0, elapsed / float(duration)),
                   eta_s=None if terminal else max(0.0, end - now),
                   expected_end=None if terminal else _from_epoch(end))
        return out

    series, scanned, rate, newest = _scan(db, run.id, points, now)
    # The final summaries can run slightly ahead of the last heartbeats.
    reported = lifecycle.live_totals(db, [run])[run.id].get("events_total") or 0
    done = max(int(scanned), int(reported))
    remaining = max(0, planned - done)
    stale = bool(active and (newest is None or now - newest > STALE_AFTER_S))
    out.update(
        kind="backfill",
        planned_events=planned,
        done_events=done,
        remaining_events=remaining,
        fraction=1.0 if run.state == lifecycle.STATE_COMPLETED else min(1.0, done / float(planned)),
        recent_eps=None if (terminal or stale) else rate,
        stale=stale,
        series=[{"ts": int(t * 1000), "done": v} for t, v in series],
    )
    if active and not stale and rate and rate > 0:
        out["eta_s"] = remaining / rate
        out["expected_end"] = _from_epoch(now + remaining / rate)
    if active and target_eps and target_eps > 0:
        out["target_end"] = _from_epoch(now + remaining / target_eps)
    return out
