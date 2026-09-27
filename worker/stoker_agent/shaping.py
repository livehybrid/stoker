"""Time-of-day rate shaping for eps runs (``rate_shape = pack``).

eps mode normally paces a flat rate and strips the pack's diurnal maps
(``hourOfDayRate``, ``dayOfWeekRate``, ``minuteOfHourRate``, ``dayOfMonthRate``,
``monthOfYearRate``) so the engine cannot under-produce against the flat
token bucket. With shaping on, the maps move from the engine into the agent:
the engine is rewritten to produce the curve's PEAK rate flat out, and the
agent's bucket follows

    rate(t) = share * factor(t) / mean_factor

so the configured eps is the run's AVERAGE and the delivered volume follows
the pack's curve. factor(t) is the product of the maps at local time t,
exactly as eventgen's rater evaluates them (Splunk weekday numbering, Sunday =
0; a key missing from a map leaves the factor unchanged). The mean is the
product of each map's mean over its whole domain, which is the true long-run
mean when the maps vary independently (hour of day vs day of week, say).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, Optional

log = logging.getLogger("stoker.shaping")

# map name -> (domain of keys, how to read the key off a struct_time)
_DOMAINS = {
    "hourOfDayRate": (range(0, 24), lambda tm: tm.tm_hour),
    "dayOfWeekRate": (range(0, 7), lambda tm: (tm.tm_wday + 1) % 7),
    "minuteOfHourRate": (range(0, 60), lambda tm: tm.tm_min),
    "dayOfMonthRate": (range(1, 32), lambda tm: tm.tm_mday),
    "monthOfYearRate": (range(1, 13), lambda tm: tm.tm_mon),
}

# Never let a zero-factor hour stop the bucket outright (TokenBucket needs a
# positive rate); this is a trickle, not a delivered volume.
MIN_RATE = 0.001


class ShapeCurve(object):
    def __init__(self, maps):
        # type: (Dict[str, Dict[int, float]]) -> None
        self.maps = maps
        mean = 1.0
        peak = 1.0
        for name, values in maps.items():
            domain = _DOMAINS[name][0]
            factors = [values.get(k, 1.0) for k in domain]
            mean *= sum(factors) / float(len(factors))
            peak *= max(factors)
        self.mean = mean
        self.peak = peak

    @property
    def peak_ratio(self):
        # type: () -> float
        """How far above the average the busiest moment runs."""
        return self.peak / self.mean

    def factor(self, epoch):
        # type: (float) -> float
        tm = time.localtime(epoch)
        f = 1.0
        for name, values in self.maps.items():
            key = _DOMAINS[name][1](tm)
            f *= values.get(key, 1.0)
        return f

    def rate(self, share_eps, epoch):
        # type: (float, float) -> float
        return max(MIN_RATE, share_eps * self.factor(epoch) / self.mean)


def _parse_map(name, text):
    # type: (str, str) -> Optional[Dict[int, float]]
    try:
        doc = json.loads(text)
    except ValueError:
        log.warning("%s is not valid JSON; ignored for shaping", name)
        return None
    if not isinstance(doc, dict):
        return None
    out = {}  # type: Dict[int, float]
    for k, v in doc.items():
        try:
            out[int(str(k).strip())] = max(0.0, float(v))
        except (TypeError, ValueError):
            continue
    return out or None


def curve_from_parser(parser):
    # type: (object) -> Optional[ShapeCurve]
    """The pack's curve: global/default maps, overridden by the first paced
    (non-replay) stanza that declares any. None when the pack has none, or
    when every factor is zero."""
    from .confrewrite import GLOBAL_SECTIONS, RATE_MAP_KEYS, _is_replay, sample_sections

    maps = {}  # type: Dict[str, Dict[int, float]]
    for section in [s for s in parser.sections() if s.lower() in GLOBAL_SECTIONS]:
        for key in RATE_MAP_KEYS:
            raw = parser.get(section, key, fallback=None)
            if raw:
                parsed = _parse_map(key, raw)
                if parsed:
                    maps[key] = parsed
    for key in RATE_MAP_KEYS:
        raw = parser.defaults().get(key)
        if raw and key not in maps:
            parsed = _parse_map(key, raw)
            if parsed:
                maps[key] = parsed
    for section in sample_sections(parser):
        if _is_replay(parser, section):
            continue
        own = {}
        for key in RATE_MAP_KEYS:
            raw = parser.get(section, key, fallback=None) if parser.has_option(section, key) else None
            if raw:
                parsed = _parse_map(key, raw)
                if parsed:
                    own[key] = parsed
        if own:
            maps.update(own)
            break
    if not maps:
        return None
    curve = ShapeCurve(maps)
    if curve.mean <= 0 or curve.peak <= 0:
        log.warning("the pack's time-of-day maps are all zero; running flat")
        return None
    return curve


def curve_from_conf(path):
    # type: (str) -> Optional[ShapeCurve]
    from .confrewrite import load_conf
    return curve_from_parser(load_conf(path))
