"""Largest-remainder apportionment of a run's rate across worker slots.

Ported from the worker's ``confrewrite.largest_remainder`` (the two must agree
so the control plane's per-worker integer split matches what the worker would
compute for its stanzas). The integer parts always sum to exactly ``total``;
ties on the fractional part resolve to the lower index (stable).

The control plane apportions the *run* rate across *workers*; the worker later
apportions *its* share across *stanzas*. Same algorithm, two levels.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence


def largest_remainder(total, weights):
    # type: (int, Sequence[float]) -> List[int]
    """Split integer ``total`` proportionally to ``weights``; parts sum exactly.

    Zero or degenerate (non-finite / all-zero) weights fall back to an equal
    split. Ties on the fractional part resolve to the lower index (stable).
    Mirrors ``worker/stoker_agent/confrewrite.largest_remainder`` exactly.
    """
    if total < 0:
        raise ValueError("total must be >= 0")
    n = len(weights)
    if n == 0:
        return []
    weight_sum = float(sum(weights))
    if weight_sum <= 0 or not math.isfinite(weight_sum):
        weights = [1.0] * n
        weight_sum = float(n)
    exact = [total * (w / weight_sum) for w in weights]
    floors = [int(math.floor(x)) for x in exact]
    shortfall = total - sum(floors)
    remainders = sorted(range(n), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in remainders[:shortfall]:
        floors[i] += 1
    return floors


def apportion_shares(rate_mode, rate_value, workers, weights=None):
    # type: (str, Optional[float], int, Optional[Sequence[float]]) -> List[Dict[str, float]]
    """Apportion a run's rate across ``workers`` slots into per-slot shares.

    Returns a list of length ``workers`` of single-key share dicts matching the
    rate mode, exactly the ``share`` object the worker's ``SpecSlice.from_claim``
    expects (one of ``eps`` | ``per_day_gb`` | ``count``):

    * ``eps`` / ``per_day_gb``: floating rate split proportionally to ``weights``
      (equal when omitted). The parts sum to ``rate_value`` (float, no rounding);
      integer EPS rounding happens later in the worker's per-stanza rewrite.
    * ``count_interval``: the integer ``count`` split by largest remainder so the
      per-slot counts sum to exactly ``rate_value`` (treated as an int total).

    ``count_interval`` with ``rate_value`` None yields ``{"count": 0}`` per slot
    (the engine's own conf ``count`` governs; the split is per-stanza in the
    worker). Raises ``ValueError`` on bad inputs.
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if weights is None:
        weights = [1.0] * workers
    if len(weights) != workers:
        raise ValueError("weights length %d != workers %d" % (len(weights), workers))

    if rate_mode in ("eps", "per_day_gb"):
        if rate_value is None or rate_value <= 0:
            raise ValueError("%s mode requires rate_value > 0" % rate_mode)
        weight_sum = float(sum(weights))
        if weight_sum <= 0 or not math.isfinite(weight_sum):
            weights = [1.0] * workers
            weight_sum = float(workers)
        key = rate_mode  # "eps" or "per_day_gb"
        shares = [{key: rate_value * (w / weight_sum)} for w in weights]
        _fix_float_residue(shares, key, rate_value)
        return shares

    if rate_mode == "count_interval":
        total = int(round(rate_value)) if rate_value else 0
        counts = largest_remainder(total, weights)
        return [{"count": float(c)} for c in counts]

    raise ValueError("unknown rate_mode %r" % rate_mode)


def _fix_float_residue(shares, key, total):
    # type: (List[Dict[str, float]], str, float) -> None
    """Push floating rounding error into the last slot so the sum is exact.

    Equal splits of e.g. 100/3 leave a tiny residue; fold it into the final
    share so ``sum(shares) == total`` holds to floating precision. The worker's
    ±1% pacing is unaffected by sub-unit adjustments; this keeps totals honest
    for the operator's estimate view.
    """
    if not shares:
        return
    current = math.fsum(s[key] for s in shares)
    shares[-1][key] += total - current
