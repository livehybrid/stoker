"""Per-engine safety ceilings for a single worker's share.

Ships as a conservative static table this stage (the DESIGN's 24 h soak that
would raise these empirically is deferred). A per-worker share above the
ceiling is rejected at submit with ``422 slice_exceeds_ceiling{suggested_workers}``,
where ``suggested_workers`` is the smallest fleet size that brings the
per-worker share under the ceiling.

eventgen ceilings (per worker): 25 GB/day and 5000 EPS. ``per_day_gb`` shares
are checked against the GB/day ceiling directly; ``eps`` against the EPS ceiling.
When a ``per_day_gb`` share is supplied with a ``bytes_per_event`` estimate we
also derive the implied EPS and check that too (whichever binds first wins).

rawreplay (Piston) reuses eventgen's per-worker ceilings: in RATE mode the agent
paces the replay with the same token bucket, so the same GB/day + EPS bounds
apply. In CADENCE mode (``count_interval``) the engine self-paces from the
recorded gaps and there is no rate ceiling (the ``count_interval`` branch below
always passes). A rawreplay run is always a single worker (the control plane
forces it), so the per-worker ceiling equals the whole-run ceiling.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Dict, Optional

# engine -> ceilings. Extend as engines are added.
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


def check_slice(rate_mode, per_worker_value, bytes_per_event=None, engine="eventgen"):
    # type: (str, Optional[float], Optional[float], str) -> CeilingCheck
    """Check one worker's share against the engine ceiling table.

    ``per_worker_value`` is this worker's share in the units of ``rate_mode``
    (EPS for ``eps``, GB/day for ``per_day_gb``). ``count_interval`` has no rate
    ceiling (engine-paced) and always passes.

    When the share exceeds a ceiling, ``suggested_workers`` is computed from the
    *total* implied by ``per_worker_value`` assuming a single worker currently
    holds it: ``ceil(per_worker_value / ceiling)``. The caller passes the
    per-worker value it already apportioned, so this answers "how many workers
    would bring each slice under the ceiling" for that same total.
    """
    ceilings = CEILINGS.get(engine)
    if ceilings is None:
        # Unknown engine: no table, do not block (documented conservative pass).
        return CeilingCheck(ok=True, detail="no ceiling table for engine %r" % engine)

    if rate_mode == "count_interval":
        return CeilingCheck(ok=True, detail="count_interval is engine-paced (no rate ceiling)")

    if per_worker_value is None or per_worker_value <= 0:
        return CeilingCheck(ok=True)

    max_eps = ceilings["max_eps_per_worker"]
    max_gb = ceilings["max_gb_day_per_worker"]

    if rate_mode == "eps":
        eps = per_worker_value
        gb_day = eps_to_gb_day(eps, bytes_per_event)
        if eps > max_eps:
            return _exceeded("eps", eps, max_eps)
        if gb_day is not None and gb_day > max_gb:
            return _exceeded("gb_day", gb_day, max_gb)
        return CeilingCheck(ok=True)

    if rate_mode == "per_day_gb":
        gb_day = per_worker_value
        eps = gb_day_to_eps(gb_day, bytes_per_event)
        if gb_day > max_gb:
            return _exceeded("gb_day", gb_day, max_gb)
        if eps is not None and eps > max_eps:
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
    """Return the ceiling table for an engine (empty dict if unknown)."""
    return dict(CEILINGS.get(engine, {}))
