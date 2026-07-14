# -*- coding: utf-8 -*-
"""Temporal patterns + value sampler for the Stoker metrics engine.

A metric's value at wall-clock time ``t`` is produced in two stages:

1. **activity(pattern, hour_of_day) -> a in [0, 1]** — a normalised daily shape.
   ``constant`` is flat; ``sine`` a 24 h sinusoid; ``business_hours`` a trapezoid;
   ``business_double_hump`` the classic 9-11am peak / lunch dip / mid-afternoon
   spike / evening tail; ``ramp`` a linear drift; ``spike`` scheduled incidents;
   ``random_walk`` a bounded mean-reverting wander (stateful, per series).

2. **sample_value(a, min, p95, max, kind, noise, rng) -> value** — maps the
   activity onto a real value. The convention (see docs) is: **min = quiet-hours
   floor, p95 = typical busy level (the pattern peaks here), max = rare ceiling**.
   ``center = min + a*(p95 - min)``; Gaussian noise (larger when busier) scatters
   around it; the result is clamped to ``[min, max]`` so occasional excursions
   approach ``max`` but never exceed it.

``kind`` interprets the sample: ``gauge`` = the value itself (CPU %, latency);
``count`` = an integer count for the interval (requests/orders per tick);
``counter`` = a monotonic cumulative total (each tick adds the interval count).

Deterministic given a seed: the same (seed, series, metric, tick) yields the same
value, so a run is reproducible and the control-plane preview matches the worker.
Stdlib only (``math`` + ``random``).
"""

from __future__ import absolute_import

import math

HOURS_PER_DAY = 24.0

# Registry of known pattern types (for validation + docs).
PATTERN_TYPES = (
    "constant",
    "sine",
    "business_hours",
    "business_double_hump",
    "ramp",
    "spike",
    "random_walk",
)

VALUE_KINDS = ("gauge", "count", "counter")

_TWO_PI = 2.0 * math.pi


def _num(params, key, default):
    # type: (dict, str, float) -> float
    """Read a numeric pattern param, tolerating strings and bad values."""
    try:
        v = params.get(key, default)
        return float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clamp01(x):
    # type: (float) -> float
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _gauss_bump(hour, centre_h, width_h):
    # type: (float, float, float) -> float
    """A Gaussian bump in [0,1] centred at ``centre_h`` hours, wrapping midnight."""
    if width_h <= 0:
        width_h = 0.5
    # Shortest circular distance on a 24 h clock.
    d = abs(hour - centre_h) % HOURS_PER_DAY
    if d > HOURS_PER_DAY / 2:
        d = HOURS_PER_DAY - d
    return math.exp(-0.5 * (d / width_h) ** 2)


def activity(pattern, hour, state=None):
    # type: (dict, float, dict) -> float
    """Normalised activity ``a in [0, 1]`` for a pattern at ``hour`` of day.

    ``pattern`` is ``{"type": <name>, ...params}``. ``state`` is an optional
    mutable dict for stateful patterns (``random_walk``) keyed per series+metric;
    it is threaded by the engine across ticks. Unknown types fall back to
    ``constant`` at level 1.0 (the linter rejects them earlier).
    """
    ptype = (pattern or {}).get("type", "constant")
    p = pattern or {}

    if ptype == "constant":
        return _clamp01(_num(p, "level", 1.0))

    if ptype == "sine":
        period = _num(p, "period_h", 24.0) or 24.0
        peak = _num(p, "peak_h", 14.0)
        trough = _num(p, "trough", 0.0)
        # cos phased so a == 1 at peak, drops to `trough` at the anti-peak.
        raw = 0.5 + 0.5 * math.cos(_TWO_PI * (hour - peak) / period)
        return _clamp01(trough + (1.0 - trough) * raw)

    if ptype == "business_hours":
        start = _num(p, "start_h", 8.0)
        end = _num(p, "end_h", 18.0)
        ramp = _num(p, "ramp_h", 1.0)
        base = _num(p, "baseline", 0.05)
        if hour <= start - ramp or hour >= end + ramp:
            core = 0.0
        elif hour < start:
            core = (hour - (start - ramp)) / ramp
        elif hour <= end:
            core = 1.0
        else:
            core = 1.0 - (hour - end) / ramp
        return _clamp01(base + (1.0 - base) * _clamp01(core))

    if ptype == "business_double_hump":
        base = _num(p, "baseline", 0.05)
        mp = _num(p, "morning_peak_h", 10.0)
        ap = _num(p, "afternoon_peak_h", 15.0)
        width = _num(p, "width_h", 1.6)
        # Afternoon hump slightly lower than morning by default; lunch dip is the
        # natural trough between the two Gaussians.
        arel = _num(p, "afternoon_rel", 0.9)
        dip = _num(p, "lunch_dip", 0.5)  # 0..1, how deep the between-humps trough sits
        morning = _gauss_bump(hour, mp, width)
        afternoon = arel * _gauss_bump(hour, ap, width)
        core = max(morning, afternoon)
        # Deepen the lunch trough: subtract a downward bump centred between peaks.
        lunch_h = (mp + ap) / 2.0
        core = core - (1.0 - dip) * _gauss_bump(hour, lunch_h, width * 0.8) * 0.5
        return _clamp01(base + (1.0 - base) * _clamp01(core))

    if ptype == "ramp":
        a0 = _num(p, "from", 0.1)
        a1 = _num(p, "to", 1.0)
        return _clamp01(a0 + (a1 - a0) * (hour / HOURS_PER_DAY))

    if ptype == "spike":
        base = _num(p, "baseline", 0.1)
        amp = _num(p, "amplitude", 0.9)
        width = _num(p, "width_h", 0.25)
        spikes = p.get("spikes_h") or p.get("at_h") or []
        if isinstance(spikes, (int, float)):
            spikes = [spikes]
        core = 0.0
        for sh in spikes:
            try:
                core = max(core, amp * _gauss_bump(hour, float(sh), width))
            except (TypeError, ValueError):
                continue
        return _clamp01(base + core)

    if ptype == "random_walk":
        base = _num(p, "baseline", 0.5)
        step = _num(p, "step", 0.05)
        revert = _num(p, "revert", 0.02)  # pull back toward baseline each tick
        if state is None:
            return _clamp01(base)
        cur = state.get("rw")
        if cur is None:
            cur = base
        rng = state.get("rng")
        drift = rng.gauss(0.0, step) if rng is not None else 0.0
        cur = cur + drift + revert * (base - cur)
        cur = _clamp01(cur)
        state["rw"] = cur
        return cur

    # Unknown pattern: flat.
    return 1.0


def sample_value(a, vmin, p95, vmax, kind, noise, rng, state=None):
    # type: (float, float, float, float, str, float, "random.Random", dict) -> float
    """Map activity ``a`` onto a value using the min/p95/max convention.

    ``center = vmin + a*(p95 - vmin)`` (quiet floor -> busy p95); Gaussian noise
    (scaled by ``noise`` and larger when busier) scatters around it; the result is
    clamped to ``[vmin, vmax]``. ``kind`` then interprets it:
    ``gauge`` -> the value; ``count`` -> a non-negative integer count for the
    interval; ``counter`` -> a monotonic cumulative total (``state['total']``).
    """
    a = _clamp01(a)
    span = (p95 - vmin)
    center = vmin + a * span
    if noise and span != 0 and rng is not None:
        sigma = abs(noise) * abs(span) * (0.3 + 0.7 * a)
        center = center + rng.gauss(0.0, sigma)
    # Clamp into the [min, max] envelope (max is the rare ceiling).
    lo, hi = (vmin, vmax) if vmax >= vmin else (vmax, vmin)
    value = min(hi, max(lo, center))

    if kind == "count":
        return float(max(0, int(round(value))))
    if kind == "counter":
        if state is None:
            return float(max(0, int(round(value))))
        delta = max(0, int(round(value)))
        total = int(state.get("total", 0)) + delta
        state["total"] = total
        return float(total)
    # gauge: keep a sensible precision.
    return round(value, 4)
