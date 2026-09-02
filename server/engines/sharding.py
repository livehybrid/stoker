"""Submit-time sharding guard: will every requested worker actually get work?

Two engines shard *discrete* work units across the fleet, and both starve the
surplus slots silently when the fleet is larger than the work:

* **metrics** — the engine owns ``series[slot::total_workers]`` (a stride shard
  of the dimension cross-product). A pack whose cross-product is 4 series run
  with 10 workers gives slots 0-3 one series each and slots 4-9 **zero**; those
  six workers connect, heartbeat healthily and emit nothing for the whole run.
* **eventgen count_interval** — the worker's conf rewrite splits each stanza's
  integer ``count`` across the fleet by largest remainder
  (``worker/stoker_agent/confrewrite._rewrite_count_interval``). A stanza with
  ``count = 6`` across 10 workers yields ``[1,1,1,1,1,1,0,0,0,0]``: slots 6-9
  emit nothing (unless some other stanza still gives them work).

Gated modes (``eps`` / ``per_day_gb``) split a continuous rate, so every slot
always receives a positive share and passes through here unaffected; rawreplay
is forced to a single worker elsewhere (``409 replay_single_worker``) and a
single-worker run trivially holds all the work.

This module answers, at submit time, "given this engine, this pack and this
worker count, how many workers will actually receive work, and why?" so the
submit route can refuse an over-provisioned run with an actionable 422 instead
of launching a fleet that half-idles at 0 EPS with no explanation. It mirrors
the :mod:`server.engines.ceilings` ``check_slice`` / ``CeilingCheck`` pattern:
a small frozen result dataclass with ``ok``, a suggested worker count, a
limiting factor and a human-readable ``detail``. The split maths reuses the
same :func:`largest_remainder` the worker uses, so the prediction here is
exactly what the fleet would do.

Wiring (for the submit route owner — this module deliberately does not touch
the route itself): in ``server/routes/api.py`` :func:`run_spec`, extend the
engines import to ``from ..engines import ceilings, sharding`` and add this
block immediately AFTER gate 3 (the ceiling check's ``raise``) and before
gate 4 (the per-target concurrent-GB cap):

    # 3b. Sharding: every requested worker must actually receive work. The
    #     metrics series stride and the count_interval largest-remainder split
    #     both leave surplus workers with nothing (healthy, heartbeating,
    #     0 EPS forever), so an over-provisioned fleet is rejected up front.
    shard = sharding.check_sharding(
        spec.engine, spec.rate_mode, spec.workers,
        metrics_config=pack.builder_config_json,
        pack_dir=pack.source_path if pack.builder_config_json is None else None)
    if not shard.ok:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "workers_exceed_shardable_work",
                "suggested_workers": shard.suggested_workers,
                "active_workers": shard.active_workers,
                "limiting_factor": shard.limiting_factor,
                "detail": shard.detail,
            },
        )
"""

from __future__ import annotations

import configparser
import dataclasses
import logging
import os
from typing import List, Optional, Sequence

from .apportion import largest_remainder

log = logging.getLogger("stoker.engines.sharding")

# Mirrors worker/stoker_agent/confrewrite.py: sections that configure the
# engine rather than describe a sample, and the conf's location in a pack.
_GLOBAL_SECTIONS = frozenset(("global", "default"))
_CONF_RELPATH = os.path.join("default", "eventgen.conf")


@dataclasses.dataclass(frozen=True)
class ShardingCheck:
    """Result of a sharding check for a run's requested worker count.

    * ``ok``: every requested worker will receive at least one unit of work
      (or the engine/mode does not shard discrete work and cannot starve).
    * ``active_workers``: how many workers would actually receive work.
    * ``total_workers``: the requested fleet size the check was run against.
    * ``suggested_workers``: largest fleet size at which no worker idles
      (``None`` when already ok).
    * ``limiting_factor``: which shard bound (``metrics_series`` |
      ``count_interval``) or ``None``.
    * ``detail``: human-readable explanation for the API error body.
    """

    ok: bool
    active_workers: int
    total_workers: int
    suggested_workers: Optional[int] = None
    limiting_factor: Optional[str] = None
    detail: Optional[str] = None


def metrics_active_workers(series_count, workers):
    # type: (int, int) -> int
    """How many of ``workers`` own at least one series under the stride shard.

    The metrics engine takes ``series[slot::total_workers]`` of the sorted
    cross-product, so exactly ``min(series_count, workers)`` slots (the low
    ones) own anything; the rest idle for the whole run.
    """
    return max(0, min(int(series_count), int(workers)))


def count_interval_active_workers(stanza_counts, workers):
    # type: (Sequence[Optional[float]], int) -> int
    """How many of ``workers`` receive a non-zero count_interval share.

    ``stanza_counts`` is one entry per *paced* (non-replay) stanza: the
    stanza's declared ``count`` (a number), or ``None`` when the stanza
    declares no count — the worker's rewrite leaves such a stanza untouched,
    so every worker emits its full sample and nobody starves. For a declared
    count the worker computes ``largest_remainder(int(count), [1.0] * total)``
    and takes its slot's part; a slot is active when ANY stanza gives it a
    positive part. The same :func:`largest_remainder` is used here so this
    prediction matches the fleet's split exactly.
    """
    workers = int(workers)
    if workers < 1:
        return 0
    if any(c is None for c in stanza_counts):
        return workers  # an undeclared-count stanza gives every slot work
    active = [False] * workers
    for count in stanza_counts:
        try:
            total = int(count)
        except (TypeError, ValueError):
            return workers  # unparseable declaration: never block on a guess
        if total < 0:
            continue
        for slot, part in enumerate(largest_remainder(total, [1.0] * workers)):
            if part > 0:
                active[slot] = True
    return sum(1 for a in active if a)


def eventgen_stanza_counts(pack_dir):
    # type: (Optional[str]) -> Optional[List[Optional[float]]]
    """Read the paced stanzas' declared ``count`` values from a pack's conf.

    Returns one entry per paced (non-replay, non-global) stanza — the parsed
    ``count`` or ``None`` when the stanza declares none — or ``None`` when the
    conf cannot be read at all (missing dir/file, parse error). A ``None``
    return makes the caller pass conservatively: this is a submit-time guard,
    and an unreadable conf is the lint gate's problem, not a reason to block
    here on a guess.
    """
    if not pack_dir:
        return None
    conf_path = os.path.join(pack_dir, _CONF_RELPATH)
    if not os.path.isfile(conf_path):
        return None
    parser = configparser.RawConfigParser(
        delimiters=("=",), strict=False, allow_no_value=True, interpolation=None)
    parser.optionxform = str  # eventgen keys are case-sensitive
    try:
        if not parser.read(conf_path, encoding="utf-8"):
            return None
    except configparser.Error as exc:
        log.info("sharding: cannot parse %s (%s); passing", conf_path, exc)
        return None
    counts = []  # type: List[Optional[float]]
    for section in parser.sections():
        if section.lower() in _GLOBAL_SECTIONS:
            continue
        if (parser.get(section, "mode", fallback="") or "").strip() == "replay":
            continue  # replay stanzas take no count share (workers = 1 anyway)
        raw = parser.get(section, "count", fallback=None)
        if raw is None:
            counts.append(None)
            continue
        try:
            counts.append(float(raw))
        except ValueError:
            counts.append(None)  # the worker's _get_float also treats this as absent
    return counts


def check_sharding(engine, rate_mode, workers, metrics_config=None, pack_dir=None,
                   pack_dirs=None):
    # type: (Optional[str], Optional[str], int, Optional[dict], Optional[str], Optional[Sequence[str]]) -> ShardingCheck
    """Check a run's worker count against the work its engine can shard.

    ``metrics_config`` is the pack's ``metricgen`` builder config (the spec is
    a metrics run exactly when the pack carries one — mirroring the submit
    route's own dispatch); ``pack_dir`` is the eventgen pack's source directory
    for the count_interval stanza scan. ``pack_dirs`` (a multi-pack spec's
    merged set, primary included) supersedes ``pack_dir``: the merged bundle's
    stanza set is the union of the packs', so the count_interval prediction
    scans every directory and concatenates the counts — one unreadable conf
    makes the whole check pass conservatively, exactly as for a single pack.
    Engines/modes that split a continuous rate (eps / per_day_gb) and
    single-worker engines (rawreplay) always pass; so does anything this
    function cannot cheaply predict (unreadable conf) — it is a guard against
    the *known* silent-starvation shapes, never a new way for a valid submit
    to fail.
    """
    workers = max(1, int(workers))
    engine = (engine or "eventgen").strip() or "eventgen"

    if workers == 1:
        return ShardingCheck(ok=True, active_workers=1, total_workers=1)

    if engine == "metrics" or metrics_config is not None:
        from ..bundles import metrics_series_count

        series = metrics_series_count(metrics_config or {})
        active = metrics_active_workers(series, workers)
        if active >= workers:
            return ShardingCheck(ok=True, active_workers=workers,
                                 total_workers=workers)
        idle = workers - active
        idle_slots = _slot_range(active, workers)
        return ShardingCheck(
            ok=False,
            active_workers=active,
            total_workers=workers,
            suggested_workers=max(1, active),
            limiting_factor="metrics_series",
            detail=(
                "the pack's dimension cross-product is %d series and the "
                "metrics engine shards series[slot::workers], so only %d of "
                "%d workers would own any series — %d worker(s) (slot%s %s) "
                "would generate nothing for the whole run; use at most %d "
                "workers" % (series, active, workers, idle,
                             "" if idle == 1 else "s", idle_slots,
                             max(1, active))),
        )

    if engine == "rawreplay":
        # Replay is forced to a single worker elsewhere (409 replay_single_worker
        # at submit, provision and scale); nothing to shard here.
        return ShardingCheck(ok=True, active_workers=workers, total_workers=workers)

    if rate_mode != "count_interval":
        # eps / per_day_gb split a continuous rate: every slot gets a positive
        # share, no worker can starve.
        return ShardingCheck(ok=True, active_workers=workers, total_workers=workers)

    if pack_dirs:
        counts = []  # type: Optional[List[Optional[float]]]
        for one_dir in pack_dirs:
            one = eventgen_stanza_counts(one_dir)
            if one is None:
                counts = None  # an unreadable conf: pass conservatively
                break
            counts.extend(one)
    else:
        counts = eventgen_stanza_counts(pack_dir)
    if counts is None or not counts:
        # Unknown stanza counts (no dir / unreadable conf / no paced stanzas):
        # pass conservatively; lint owns conf validity.
        return ShardingCheck(ok=True, active_workers=workers, total_workers=workers,
                             detail="stanza counts unknown; sharding not checked")
    active = count_interval_active_workers(counts, workers)
    if active >= workers:
        return ShardingCheck(ok=True, active_workers=workers, total_workers=workers)
    idle = workers - active
    idle_slots = _slot_range(active, workers)
    total_count = sum(int(c) for c in counts if c is not None and c >= 0)
    return ShardingCheck(
        ok=False,
        active_workers=active,
        total_workers=workers,
        suggested_workers=max(1, active),
        limiting_factor="count_interval",
        detail=(
            "count_interval splits each stanza's integer count across the "
            "fleet by largest remainder; this pack's stanzas declare %d "
            "event(s) per interval in total, so only %d of %d workers would "
            "receive any count — %d worker(s) (slot%s %s) would generate "
            "nothing for the whole run; use at most %d workers"
            % (total_count, active, workers, idle,
               "" if idle == 1 else "s", idle_slots, max(1, active))),
    )


def _slot_range(first_idle, workers):
    # type: (int, int) -> str
    """Format the idle slot span for a detail message (e.g. ``4-9`` or ``3``).

    Both starvation shapes leave a contiguous *suffix* of slots idle (the
    stride and the equal-weight largest remainder each favour the low slots),
    so the span is always ``first_idle .. workers-1``.
    """
    if first_idle >= workers - 1:
        return str(workers - 1)
    return "%d-%d" % (first_idle, workers - 1)


__all__ = [
    "ShardingCheck",
    "check_sharding",
    "metrics_active_workers",
    "count_interval_active_workers",
    "eventgen_stanza_counts",
]
