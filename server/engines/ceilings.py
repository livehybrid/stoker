"""Per-engine safety ceilings for a single worker's share.

The built-in table below is a conservative default (the DESIGN's 24 h soak that
would raise it empirically never ran, and real workers routinely beat 25 GB/day
when the data is cheap to template and Splunk is close on the network). It is
therefore only the LOWEST layer of a three-layer resolution, most specific wins:

1. **Per-fleet override** — a fleet row's ``config_json`` may carry
   ``max_eps_per_worker`` / ``max_gb_day_per_worker`` (a fleet of beefy nodes on
   the target's LAN can go much harder than a fleet of small ones).
2. **Environment** — ``STOKER_MAX_EPS_PER_WORKER`` /
   ``STOKER_MAX_GB_DAY_PER_WORKER`` set the global per-worker defaults, and
   ``STOKER_MAX_EPS_PER_WORKER_<ENGINE>`` / ``STOKER_MAX_GB_DAY_PER_WORKER_<ENGINE>``
   narrow them per engine (parsed in :mod:`server.config`).
3. **Built-in table** — the values below, so an unconfigured deployment behaves
   exactly as before.

At any layer a value of ``0`` (or negative) DISABLES that bound entirely — an
operator who has measured a worker at 200 GB/day can turn the guard off rather
than lie to it. A disabled bound resolves to ``None`` and is simply skipped by
:func:`check_slice` (no division, no comparison). :func:`resolve_ceilings`
performs the layering; the API passes its result to both the submit guard and
the estimate view so they can never disagree.

A per-worker share above the resolved ceiling is rejected at submit with
``422 slice_exceeds_ceiling{suggested_workers}``, where ``suggested_workers`` is
the smallest fleet size that brings the per-worker share under the ceiling.

eventgen default ceilings (per worker): 25 GB/day and 5000 EPS. ``per_day_gb``
shares are checked against the GB/day ceiling directly; ``eps`` against the EPS
ceiling. When a ``per_day_gb`` share is supplied with a ``bytes_per_event``
estimate we also derive the implied EPS and check that too (whichever binds
first wins).

rawreplay (Piston) reuses eventgen's per-worker ceilings: in RATE mode the agent
paces the replay with the same token bucket, so the same GB/day + EPS bounds
apply. In CADENCE mode (``count_interval``) the engine self-paces from the
recorded gaps and there is no rate ceiling (the ``count_interval`` branch below
always passes). A rawreplay run is always a single worker (the control plane
forces it), so the per-worker ceiling equals the whole-run ceiling.

An engine absent from the built-in table (e.g. ``metrics``) has no ceiling at
all and always passes — the documented conservative behaviour, preserved by
:func:`resolve_ceilings` returning ``None`` for it.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Dict, Mapping, Optional

# engine -> built-in default ceilings (the lowest layer of resolve_ceilings).
# Extend as engines are added.
CEILINGS = {
    "eventgen": {
        "max_gb_day_per_worker": 25.0,
        "max_eps_per_worker": 5000.0,
    },
    # rawreplay reuses eventgen's per-worker bounds (documented in the module
    # docstring): in RATE mode the same token bucket paces it, and a replay run
    # is always workers=1 so the per-worker ceiling is the whole-run ceiling.
    "rawreplay": {
        "max_gb_day_per_worker": 25.0,
        "max_eps_per_worker": 5000.0,
    },
}

# The two bound keys, shared by the built-in table, the Settings fields and the
# per-fleet config_json override (one vocabulary everywhere on purpose).
CEILING_KEYS = ("max_gb_day_per_worker", "max_eps_per_worker")

SECONDS_PER_DAY = 86400.0
BYTES_PER_GB = 1_000_000_000.0  # decimal GB, matching eventgen perDayVolume


@dataclasses.dataclass
class CeilingCheck:
    """Result of a ceiling check for one worker's share.

    * ``ok``: the share is within the engine's ceilings.
    * ``suggested_workers``: smallest fleet size that would make each worker's
      share fit (``None`` when already ok or when the mode has no ceiling).
    * ``limiting_factor``: which ceiling bound (``eps`` | ``gb_day``) or ``None``.
    * ``detail``: human-readable explanation for the API error body.
    """

    ok: bool
    suggested_workers: Optional[int] = None
    limiting_factor: Optional[str] = None
    detail: Optional[str] = None


def gb_day_to_eps(per_day_gb, bytes_per_event):
    # type: (float, Optional[float]) -> Optional[float]
    """Convert a GB/day volume to an approximate EPS given bytes/event.

    Returns ``None`` when ``bytes_per_event`` is unknown or non-positive (the
    conversion is then undefined and only the GB/day ceiling applies).
    """
    if not bytes_per_event or bytes_per_event <= 0:
        return None
    bytes_per_day = per_day_gb * BYTES_PER_GB
    return (bytes_per_day / bytes_per_event) / SECONDS_PER_DAY


def eps_to_gb_day(eps, bytes_per_event):
    # type: (float, Optional[float]) -> Optional[float]
    """Convert an EPS rate to GB/day given bytes/event (None if unknown)."""
    if not bytes_per_event or bytes_per_event <= 0:
        return None
    return (eps * bytes_per_event * SECONDS_PER_DAY) / BYTES_PER_GB


def _normalise_bound(value):
    # type: (Any) -> Optional[float]
    """A configured bound value -> effective bound. <= 0 means DISABLED (None)."""
    value = float(value)
    return value if value > 0 else None


def resolve_ceilings(engine="eventgen", settings=None, fleet_config=None):
    # type: (str, Optional[Any], Optional[Mapping[str, Any]]) -> Optional[Dict[str, Optional[float]]]
    """Resolve the effective per-worker ceilings for ``engine``.

    Layers, most specific wins: the fleet row's ``config_json`` override, then
    the environment (``settings`` — global fields plus per-engine entries), then
    the built-in :data:`CEILINGS` table. Returns ``None`` for an engine with no
    built-in table (no ceiling at all — the documented conservative pass), else
    a dict with both :data:`CEILING_KEYS`, where a value of ``None`` means that
    bound is DISABLED (a configured 0/negative at any layer).

    ``settings`` is duck-typed (``max_eps_per_worker``, ``max_gb_day_per_worker``
    and ``per_engine_ceilings`` attributes, all optional) so this module stays a
    pure function with no import of :mod:`server.config`; the API passes the
    real :class:`~server.config.Settings`.
    """
    table = CEILINGS.get(engine)
    if table is None:
        return None
    resolved = {key: table.get(key) for key in CEILING_KEYS}  # type: Dict[str, Optional[float]]

    if settings is not None:
        # Global env defaults (None = unset, keep the built-in value).
        for key in CEILING_KEYS:
            value = getattr(settings, key, None)
            if value is not None:
                resolved[key] = _normalise_bound(value)
        # Per-engine env overrides beat the globals.
        for eng, key, value in getattr(settings, "per_engine_ceilings", ()) or ():
            if eng == engine and key in resolved:
                resolved[key] = _normalise_bound(value)

    if fleet_config:
        # Per-fleet override beats everything (same key names as the table; a
        # JSON null is treated as absent — set 0 to disable a bound).
        for key in CEILING_KEYS:
            value = fleet_config.get(key)
            if value is not None:
                resolved[key] = _normalise_bound(value)

    return resolved


def check_slice(rate_mode, per_worker_value, bytes_per_event=None, engine="eventgen",
                ceilings=None):
    # type: (str, Optional[float], Optional[float], str, Optional[Dict[str, Optional[float]]]) -> CeilingCheck
    """Check one worker's share against the engine's (resolved) ceilings.

    ``per_worker_value`` is this worker's share in the units of ``rate_mode``
    (EPS for ``eps``, GB/day for ``per_day_gb``). ``count_interval`` has no rate
    ceiling (engine-paced) and always passes.

    ``ceilings`` is the effective table from :func:`resolve_ceilings`; the API
    passes the same resolved table here and to the estimate view so the two
    always agree. When omitted, the built-in defaults are used (no env, no
    fleet override — the historical behaviour). A bound of ``None`` inside the
    table is DISABLED and never checked, so a fully-disabled table passes
    everything without touching the :func:`_exceeded` maths.

    When the share exceeds a ceiling, ``suggested_workers`` is computed from the
    *total* implied by ``per_worker_value`` assuming a single worker currently
    holds it: ``ceil(per_worker_value / ceiling)``. The caller passes the
    per-worker value it already apportioned, so this answers "how many workers
    would bring each slice under the ceiling" for that same total.
    """
    if ceilings is None:
        ceilings = resolve_ceilings(engine)
    if ceilings is None:
        # Unknown engine: no table, do not block (documented conservative pass).
        return CeilingCheck(ok=True, detail="no ceiling table for engine %r" % engine)

    if rate_mode == "count_interval":
        return CeilingCheck(ok=True, detail="count_interval is engine-paced (no rate ceiling)")

    if per_worker_value is None or per_worker_value <= 0:
        return CeilingCheck(ok=True)

    max_eps = ceilings.get("max_eps_per_worker")
    max_gb = ceilings.get("max_gb_day_per_worker")

    if rate_mode == "eps":
        eps = per_worker_value
        gb_day = eps_to_gb_day(eps, bytes_per_event)
        if max_eps is not None and eps > max_eps:
            return _exceeded("eps", eps, max_eps)
        if max_gb is not None and gb_day is not None and gb_day > max_gb:
            return _exceeded("gb_day", gb_day, max_gb)
        return CeilingCheck(ok=True)

    if rate_mode == "per_day_gb":
        gb_day = per_worker_value
        eps = gb_day_to_eps(gb_day, bytes_per_event)
        if max_gb is not None and gb_day > max_gb:
            return _exceeded("gb_day", gb_day, max_gb)
        if max_eps is not None and eps is not None and eps > max_eps:
            return _exceeded("eps", eps, max_eps)
        return CeilingCheck(ok=True)

    raise ValueError("unknown rate_mode %r" % rate_mode)


def _exceeded(factor, value, ceiling):
    # type: (str, float, float) -> CeilingCheck
    suggested = int(math.ceil(value / ceiling))
    if suggested < 2:
        suggested = 2  # already over at 1 worker; at least 2 needed
    unit = "EPS" if factor == "eps" else "GB/day"
    return CeilingCheck(
        ok=False,
        suggested_workers=suggested,
        limiting_factor=factor,
        detail=(
            "per-worker %s %.2f exceeds the %s ceiling of %.2f; "
            "use at least %d workers" % (unit, value, unit, ceiling, suggested)
        ),
    )


def ceiling_for(engine="eventgen"):
    # type: (str) -> Dict[str, float]
    """Return the BUILT-IN ceiling table for an engine (empty dict if unknown).

    Defaults only — env and fleet overrides are applied by
    :func:`resolve_ceilings`, which callers wanting the effective values should
    use instead. Kept for compatibility (and for the "what would the default
    be" question).
    """
    return dict(CEILINGS.get(engine, {}))
