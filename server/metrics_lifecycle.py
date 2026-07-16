"""Metric-sample lifecycle (roll-up + prune) and optional dogfood telemetry.

Two independent, failure-isolated concerns own this module:

1. **Roll-up + prune** (:func:`roll_up_and_prune`). A long soak at many workers
   appends a ``metric_samples`` row per slot every ~5 s; left unbounded that
   table bloats without limit. The maintenance pass down-samples fine-grained
   rows older than the roll-up window (48 h by default) to one aggregated row
   per slot per 60 s bucket, then hard-deletes anything older than the prune
   window (30 days). Both windows are config-driven. The big delete is chunked
   by primary key and committed per batch so it never holds one giant
   transaction or blocks the fast supervisor loop.

   Bucketing is done in Python (group by ``slot`` and ``epoch // bucket_s``),
   not in SQL, because ``date_trunc`` is Postgres-only and the suite runs on
   SQLite. The DB stays the source of truth: the aggregate row replaces exactly
   the fine-grained rows it summarises, inside one transaction per bucket batch.

2. **Dogfood telemetry** (:func:`emit_run_transition_event`,
   :func:`emit_run_metrics`, :func:`emit_hec_events`). When
   ``settings.dogfood_enabled`` the control plane ships its own observability to
   Splunk over HEC: a ``stoker:job`` event on every run state transition and a
   periodic ``stoker:metrics`` aggregate per active run. Entirely optional and
   best-effort: a HEC failure is swallowed (logged without the token) and never
   propagates into the caller. The token is never logged.

The public entry points the lifespan wires are :func:`roll_up_and_prune` (slow
maintenance loop) and :func:`emit_active_run_metrics` (periodic dogfood metrics
across every active run). :func:`emit_run_transition_event` is called from
``lifecycle.transition_run`` so a transition emits its job event when dogfood is
on. Every dogfood path is a no-op when dogfood is disabled.
"""

from __future__ import annotations

import datetime
import gzip
import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import delete, select

from .config import Settings, get_settings
from .models import MetricSample, Run, WorkerLease, utcnow

log = logging.getLogger("stoker.metrics")

# Active run states an aggregate is worth emitting for (a live fleet exists).
_ACTIVE_STATES = ("provisioning", "releasing", "running", "draining")
# Lease states that count as "live" for a per-run aggregate (a worker holding
# the slot and reporting counters); lost/done/free carry no fresh telemetry.
_LIVE_LEASE_STATES = ("claimed", "ready", "running")

# Counter fields aggregated over the fine-grained rows in a roll-up bucket.
# last:  a monotonic cumulative counter -> keep the final value in the bucket.
# mean:  an instantaneous gauge -> average across the bucket's samples.
# sum:   a per-interval delta -> add across the bucket.
#
# The agent reports EVERY HEC counter (2xx/4xx/5xx/timeouts/retries) as a running
# cumulative total, exactly like events_total/bytes_total (see the worker's
# hec_client: the counters only ever ``+=`` and are never reset per heartbeat).
# So they roll up as ``last`` — summing them across a bucket would multiply the
# true value by the sample count (~12x at a 60 s bucket / 5 s heartbeat). There
# are no genuinely per-interval delta columns, so _ROLLUP_SUM is empty.
_ROLLUP_LAST = ("events_total", "bytes_total", "queue_depth",
                "hec_2xx", "hec_4xx", "hec_5xx", "hec_timeouts", "retries")
_ROLLUP_MEAN = ("eps", "bps", "lag_s", "rss_mb", "cpu_pct")
_ROLLUP_SUM = ()


# --------------------------------------------------------------------------- #
# Roll-up + prune.
# --------------------------------------------------------------------------- #

def roll_up_and_prune(db, settings=None, now=None):
    # type: (Any, Optional[Settings], Optional[datetime.datetime]) -> Dict[str, int]
    """Down-sample old fine-grained metric_samples, then prune ancient rows.

    Two phases against ``metric_samples``:

    1. **Roll-up**: rows older than ``metric_rollup_after_h`` that are not
       already a bucket aggregate are grouped by ``(run_id, slot, epoch //
       bucket_s)`` and replaced with one aggregated row per bucket (``last`` of
       the cumulative counters, ``mean`` of the gauges, ``sum`` of the per-
       interval deltas). A single-sample bucket is left untouched (nothing to
       gain). The aggregate row's ``ts`` is the bucket floor.
    2. **Prune**: rows older than ``metric_prune_after_d`` are hard-deleted,
       chunked by primary key (``metric_delete_chunk`` per batch) and committed
       per batch so the delete never holds one large transaction.

    The caller need not hold a transaction open: this commits its own work in
    bounded batches. Returns a small counter dict for logging/telemetry
    (``rolled_up`` rows removed, ``aggregates`` rows written, ``pruned`` rows
    deleted).

    Args:
        db: an active SQLAlchemy ``Session``.
        settings: config (defaults to :func:`get_settings`).
        now: the reference instant (defaults to :func:`utcnow`; injectable for
            tests).

    Returns:
        ``{"rolled_up": int, "aggregates": int, "pruned": int}``.
    """
    if settings is None:
        settings = get_settings()
    if now is None:
        now = utcnow()
    now = _as_aware(now)

    bucket_s = max(1, int(settings.metric_rollup_bucket_s))
    rollup_before = now - datetime.timedelta(hours=float(settings.metric_rollup_after_h))
    prune_before = now - datetime.timedelta(days=float(settings.metric_prune_after_d))
    chunk = max(1, int(settings.metric_delete_chunk))

    # Prune first: hard-delete ancient rows before the roll-up scans, so the
    # roll-up never wastes work bucketing rows that are about to be deleted (and
    # the roll-up's read set is the smaller "older than 48 h but within 30 d"
    # band). Prune's own window (30 d) is well past the roll-up window (48 h).
    pruned = _prune(db, prune_before, chunk)
    rolled_up, aggregates = _roll_up(db, rollup_before, bucket_s, chunk)

    if rolled_up or pruned:
        log.info("metric maintenance: rolled up %d rows -> %d aggregates, pruned %d rows",
                 rolled_up, aggregates, pruned)
    return {"rolled_up": rolled_up, "aggregates": aggregates, "pruned": pruned}


def _roll_up(db, rollup_before, bucket_s, chunk):
    # type: (Any, datetime.datetime, int, int) -> Tuple[int, int]
    """Down-sample fine-grained rows older than ``rollup_before`` into buckets.

    Rows are read oldest-first and grouped by ``(run_id, slot, bucket)`` where
    ``bucket = epoch // bucket_s``. Each multi-row bucket is collapsed to one
    aggregated row (the originals deleted, the aggregate inserted) inside a
    per-bucket-batch transaction so the work is bounded. Already-aggregated
    rows are skipped (a bucket that is a single row is left as-is), so the pass
    is idempotent: re-running it never re-buckets an aggregate.

    Returns ``(fine_rows_removed, aggregate_rows_written)``.
    """
    # Pull only the columns we aggregate (id + ts + counters), oldest first.
    # Reading eagerly (not row-streaming) keeps the grouping simple; the roll-up
    # window means this set is bounded to "older than 48 h" and shrinks every
    # pass as buckets collapse, so it converges rather than growing.
    stmt = (
        select(MetricSample)
        .where(MetricSample.ts < rollup_before)
        .order_by(MetricSample.ts.asc())
    )
    rows = list(db.execute(stmt).scalars().all())
    if not rows:
        return 0, 0

    # Group into 60 s buckets keyed by run+slot+bucket-index.
    buckets = {}  # type: Dict[Tuple[int, int, int], List[MetricSample]]
    for row in rows:
        ts = _as_aware(row.ts)
        bucket_idx = int(ts.timestamp()) // bucket_s
        key = (row.run_id, row.slot, bucket_idx)
        buckets.setdefault(key, []).append(row)

    removed = 0
    written = 0
    # Collapse each multi-sample bucket. Batch the DB work by row count so a huge
    # backlog is committed incrementally rather than in one transaction.
    pending_since_commit = 0
    for (run_id, slot, bucket_idx), members in buckets.items():
        if len(members) < 2:
            continue  # a lone sample is already minimal; leave it be.
        agg = _aggregate_bucket(run_id, slot, bucket_idx, bucket_s, members)
        for member in members:
            db.delete(member)
        db.add(agg)
        removed += len(members)
        written += 1
        pending_since_commit += len(members)
        if pending_since_commit >= chunk:
            db.commit()
            pending_since_commit = 0
    if pending_since_commit:
        db.commit()
    return removed, written


def _aggregate_bucket(run_id, slot, bucket_idx, bucket_s, members):
    # type: (int, int, int, int, Sequence[MetricSample]) -> MetricSample
    """Collapse a bucket's samples into one aggregated :class:`MetricSample`.

    ``last`` cumulative counters take the final (latest-ts) member's value;
    ``mean`` gauges average their non-null values; ``sum`` deltas add their
    non-null values. The aggregate's ``ts`` is the bucket floor (``bucket_idx *
    bucket_s`` as UTC) so it sorts where the bucket lived.
    """
    ordered = sorted(members, key=lambda m: _as_aware(m.ts))
    last = ordered[-1]
    kwargs = {}  # type: Dict[str, Any]
    for field in _ROLLUP_LAST:
        kwargs[field] = getattr(last, field)
    for field in _ROLLUP_MEAN:
        kwargs[field] = _mean(getattr(m, field) for m in ordered)
    for field in _ROLLUP_SUM:
        kwargs[field] = _sum(getattr(m, field) for m in ordered)
    bucket_ts = datetime.datetime.fromtimestamp(bucket_idx * bucket_s,
                                                tz=datetime.timezone.utc)
    return MetricSample(run_id=run_id, slot=slot, ts=bucket_ts, **kwargs)


def _prune(db, prune_before, chunk):
    # type: (Any, datetime.datetime, int) -> int
    """Hard-delete metric_samples older than ``prune_before`` in id-chunked batches.

    Deletes by primary key in batches of ``chunk`` and commits per batch, so a
    multi-million-row prune never holds one long-running transaction that could
    block the supervisor's writes. Returns the total number of rows deleted.
    """
    total = 0
    while True:
        ids = list(db.execute(
            select(MetricSample.id)
            .where(MetricSample.ts < prune_before)
            .order_by(MetricSample.id.asc())
            .limit(chunk)
        ).scalars().all())
        if not ids:
            break
        db.execute(delete(MetricSample).where(MetricSample.id.in_(ids)))
        db.commit()
        total += len(ids)
        if len(ids) < chunk:
            break
    return total


# --------------------------------------------------------------------------- #
# Dogfood telemetry: HEC emitter + event builders.
# --------------------------------------------------------------------------- #

def emit_run_transition_event(run, from_state, to_state, settings=None, extra=None):
    # type: (Run, Optional[str], str, Optional[Settings], Optional[Mapping[str, Any]]) -> None
    """Emit a ``stoker:job`` event for a run state transition (dogfood only).

    A no-op unless ``settings.dogfood_enabled``. Best-effort: a HEC failure is
    swallowed. Never raises into the caller (``lifecycle.transition_run``) and
    never logs the token.
    """
    if settings is None:
        settings = get_settings()
    if not settings.dogfood_enabled:
        return
    body = {
        "type": "run_transition",
        "run_id": run.id,
        "spec_id": run.spec_id,
        "from": from_state,
        "to": to_state,
        "end_reason": run.end_reason,
        "degraded": bool(run.degraded),
    }
    if extra:
        body.update(dict(extra))
    event = _hec_envelope("stoker:job", body, run=run, settings=settings)
    emit_hec_events([event], settings=settings)


def emit_run_metrics(db, run, settings=None, now=None):
    # type: (Any, Run, Optional[Settings], Optional[datetime.datetime]) -> bool
    """Emit one ``stoker:metrics`` aggregate for a single active run (dogfood).

    Aggregates the latest live-lease telemetry across the run (summing eps/bps
    and HEC 2xx/4xx/5xx/timeouts/retries over each live lease's most recent
    sample). A no-op returning ``False`` when dogfood is disabled, the run is
    not active, or it has no live-lease samples to report. Best-effort: a HEC
    failure is swallowed and never raises.
    """
    if settings is None:
        settings = get_settings()
    if not settings.dogfood_enabled:
        return False
    if run.state not in _ACTIVE_STATES:
        return False
    agg = _aggregate_run_metrics(db, run)
    if agg is None:
        return False
    event = _hec_envelope("stoker:metrics", agg, run=run, settings=settings, ts=now)
    return emit_hec_events([event], settings=settings)


def emit_active_run_metrics(db, settings=None, now=None):
    # type: (Any, Optional[Settings], Optional[datetime.datetime]) -> int
    """Emit a ``stoker:metrics`` aggregate for every active run (dogfood loop).

    The periodic dogfood metrics pass the lifespan drives. A no-op returning 0
    when dogfood is disabled. Each run's emit is isolated: one run's HEC failure
    (already swallowed inside :func:`emit_run_metrics`) never stops the others.
    Batches all runs' events into one HEC POST when possible for efficiency.
    Returns the number of runs an event was built for.
    """
    if settings is None:
        settings = get_settings()
    if not settings.dogfood_enabled:
        return 0
    runs = list(db.execute(
        select(Run).where(Run.state.in_(_ACTIVE_STATES))
    ).scalars().all())
    events = []  # type: List[Dict[str, Any]]
    for run in runs:
        try:
            agg = _aggregate_run_metrics(db, run)
        except Exception as exc:  # one bad run must not stop the batch
            log.debug("dogfood: run %s metrics aggregate failed: %s", run.id, exc)
            continue
        if agg is None:
            continue
        events.append(_hec_envelope("stoker:metrics", agg, run=run,
                                    settings=settings, ts=now))
    if events:
        emit_hec_events(events, settings=settings)
    return len(events)


def _aggregate_run_metrics(db, run):
    # type: (Any, Run) -> Optional[Dict[str, Any]]
    """Aggregate a run's live-lease telemetry from each lease's latest sample.

    Returns the event body (or ``None`` when the run has no live leases with a
    sample). Sums the rate + HEC-outcome counters across the live leases so the
    aggregate is the run's current whole-fleet throughput and error posture.
    """
    leases = list(db.execute(
        select(WorkerLease).where(WorkerLease.run_id == run.id)
    ).scalars().all())
    live_slots = [l.slot for l in leases if l.state in _LIVE_LEASE_STATES]
    if not live_slots:
        return None

    eps = 0.0
    bps = 0.0
    events_total = 0
    bytes_total = 0
    hec = {"hec_2xx": 0, "hec_4xx": 0, "hec_5xx": 0, "hec_timeouts": 0, "retries": 0}
    lag_vals = []  # type: List[float]
    reporting = 0
    for slot in live_slots:
        sample = _latest_sample(db, run.id, slot)
        if sample is None:
            continue
        reporting += 1
        eps += sample.eps or 0.0
        bps += sample.bps or 0.0
        events_total += sample.events_total or 0
        bytes_total += sample.bytes_total or 0
        for key in hec:
            hec[key] += getattr(sample, key) or 0
        if sample.lag_s is not None:
            lag_vals.append(float(sample.lag_s))
    if reporting == 0:
        return None

    body = {
        "type": "run_metrics",
        "run_id": run.id,
        "spec_id": run.spec_id,
        "state": run.state,
        "live_workers": len(live_slots),
        "reporting_workers": reporting,
        "eps": round(eps, 3),
        "bps": round(bps, 3),
        "events_total": events_total,
        "bytes_total": bytes_total,
        "lag_s_max": round(max(lag_vals), 3) if lag_vals else None,
    }
    body.update(hec)
    return body


def _latest_sample(db, run_id, slot):
    # type: (Any, int, int) -> Optional[MetricSample]
    """Return the most recent metric_sample for a run's slot (or None)."""
    return db.execute(
        select(MetricSample)
        .where(MetricSample.run_id == run_id, MetricSample.slot == slot)
        .order_by(MetricSample.ts.desc())
        .limit(1)
    ).scalars().first()


def _hec_envelope(sourcetype, body, run=None, settings=None, ts=None):
    # type: (str, Mapping[str, Any], Optional[Run], Optional[Settings], Optional[datetime.datetime]) -> Dict[str, Any]
    """Build a Splunk HEC event envelope (``{"time","event","sourcetype",...}``).

    ``time`` is epoch seconds (float). ``source`` is ``stoker:control-plane`` and
    ``host`` is the public base URL's host so dogfooded events are attributable
    to this control plane. The token is *not* part of the envelope (it rides in
    the ``Authorization`` header only).
    """
    if settings is None:
        settings = get_settings()
    when = _as_aware(ts) if ts is not None else utcnow()
    envelope = {
        "time": when.timestamp(),
        "source": "stoker:control-plane",
        "sourcetype": sourcetype,
        "event": dict(body),
    }  # type: Dict[str, Any]
    host = _host_of(settings.public_base_url)
    if host:
        envelope["host"] = host
    return envelope


def emit_hec_events(events, settings=None):
    # type: (Sequence[Mapping[str, Any]], Optional[Settings]) -> bool
    """POST HEC event envelopes to the dogfood collector, best-effort.

    Serialises ``events`` as the newline-delimited JSON the HEC ``/event``
    endpoint accepts (one envelope per line), optionally gzips the body, and
    POSTs with the dogfood token in the ``Authorization: Splunk <token>``
    header. **Never raises** and **never logs the token**: any failure (no
    config, network error, non-2xx) is swallowed with a token-free debug line
    and returns ``False``. Returns ``True`` on a 2xx.

    This is the single choke point every dogfood emit goes through, so failure
    isolation and secret-hygiene live in exactly one place.
    """
    if settings is None:
        settings = get_settings()
    if not settings.dogfood_enabled or not events:
        return False
    url = settings.dogfood_hec_url.rstrip("/") + "/services/collector/event"
    # NDJSON: one HEC envelope per line (the collector reads concatenated events).
    payload = "\n".join(json.dumps(e, separators=(",", ":")) for e in events)
    data = payload.encode("utf-8")  # type: bytes
    headers = {
        # The token is confined to this header and never logged.
        "Authorization": "Splunk %s" % settings.dogfood_hec_token,
        "Content-Type": "application/json",
    }
    if settings.dogfood_gzip:
        try:
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        except Exception:  # gzip should never fail on bytes; fall back to raw
            data = payload.encode("utf-8")
            headers.pop("Content-Encoding", None)
    try:
        import httpx

        with httpx.Client(timeout=5.0, verify=True) as client:
            resp = client.post(url, content=data, headers=headers)
        if 200 <= resp.status_code < 300:
            return True
        # Log the status only (never the body — it can echo the token back — and
        # never the header). URL host without query is safe.
        log.debug("dogfood HEC POST returned HTTP %d", resp.status_code)
        return False
    except Exception as exc:
        # Best-effort telemetry: a HEC hiccup must never disturb the control
        # plane. Log the exception type only; the message could carry the URL
        # (safe) but we keep it terse and token-free.
        log.debug("dogfood HEC POST failed: %s", type(exc).__name__)
        return False


# --------------------------------------------------------------------------- #
# Small helpers.
# --------------------------------------------------------------------------- #

def _mean(values):
    # type: (Any) -> Optional[float]
    """Mean of the non-null values (``None`` when none are present)."""
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _sum(values):
    # type: (Any) -> Optional[int]
    """Sum of the non-null values (``None`` when none are present).

    Kept integer for the count-style delta counters (hec_*/retries). A null-only
    bucket yields ``None`` rather than 0 so an absent counter stays absent.
    """
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return int(sum(nums))


def _host_of(base_url):
    # type: (Optional[str]) -> Optional[str]
    """Extract the host[:port] from a base URL (best-effort; None on failure)."""
    if not base_url:
        return None
    try:
        from urllib.parse import urlparse

        return urlparse(base_url).netloc or None
    except Exception:
        return None


def _as_aware(value):
    # type: (datetime.datetime) -> datetime.datetime
    """Coerce a datetime to tz-aware UTC (SQLite round-trips lose the tzinfo)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


__all__ = [
    "roll_up_and_prune",
    "emit_run_transition_event",
    "emit_run_metrics",
    "emit_active_run_metrics",
    "emit_hec_events",
]
