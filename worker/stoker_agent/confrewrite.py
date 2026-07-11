"""Conf rewrite rules per docs/WORKER-CONTRACT.md.

Pure functions over a RawConfigParser (optionxform=str, '=' delimiter,
non-strict, no interpolation). The rewrite strips output-side keys, sets
outputMode = stoker, stamps sampleDir and apportions this worker's share
across stanzas by largest remainder. Replay stanzas keep their pacing keys
untouched and take no share (the control plane guarantees workers = 1 for
replay).
"""

from __future__ import annotations

import configparser
import logging
import math
from typing import Dict, List, Optional, Sequence

log = logging.getLogger("stoker.confrewrite")

# Output-side keys the agent owns; metadata is stamped from slice overrides.
OUTPUT_KEYS_EXACT = frozenset((
    "outputMode", "splunkHost", "splunkPort", "splunkMethod",
    "index", "sourcetype", "source", "host",
))
OUTPUT_KEY_PREFIXES = ("httpevent",)

# Diurnal shaping maps. eps mode is a flat instantaneous rate, so these are
# stripped there (a shaped engine would under-produce and starve the flat
# token bucket, breaching +/-1%); per_day_gb and count_interval preserve them.
RATE_MAP_KEYS = frozenset((
    "hourOfDayRate", "dayOfWeekRate", "minuteOfHourRate",
    "dayOfMonthRate", "monthOfYearRate",
))

# Sections that configure the engine rather than describe a sample.
GLOBAL_SECTIONS = frozenset(("global", "default"))

EVENTGEN_DEFAULT_INTERVAL_S = 60.0


class ConfRewriteError(Exception):
    pass


def make_parser():
    # type: () -> configparser.RawConfigParser
    parser = configparser.RawConfigParser(
        delimiters=("=",), strict=False, allow_no_value=True,
        interpolation=None,
    )
    parser.optionxform = str  # eventgen keys are case-sensitive
    return parser


def load_conf(path):
    # type: (str) -> configparser.RawConfigParser
    parser = make_parser()
    read = parser.read(path, encoding="utf-8")
    if not read:
        raise ConfRewriteError("cannot read conf file %r" % path)
    return parser


def write_conf(parser, path):
    # type: (configparser.RawConfigParser, str) -> None
    with open(path, "w", encoding="utf-8") as fh:
        parser.write(fh)


def largest_remainder(total, weights):
    # type: (int, Sequence[float]) -> List[int]
    """Split integer `total` proportionally to `weights`; parts sum exactly.

    Zero or degenerate weights fall back to an equal split. Ties on the
    fractional part resolve to the lower index (stable).
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


def _is_replay(parser, section):
    # type: (configparser.RawConfigParser, str) -> bool
    try:
        return parser.get(section, "mode", fallback="").strip() == "replay"
    except configparser.Error:
        return False


def sample_sections(parser):
    # type: (configparser.RawConfigParser) -> List[str]
    return [s for s in parser.sections() if s.lower() not in GLOBAL_SECTIONS]


def _strip_output_keys(parser):
    # type: (configparser.RawConfigParser) -> None
    for key in list(parser.defaults().keys()):
        if key in OUTPUT_KEYS_EXACT or key.startswith(OUTPUT_KEY_PREFIXES):
            del parser.defaults()[key]
    for section in parser.sections():
        for key in list(parser.options(section)):
            if key in OUTPUT_KEYS_EXACT or key.startswith(OUTPUT_KEY_PREFIXES):
                parser.remove_option(section, key)


def _strip_rate_maps(parser):
    # type: (configparser.RawConfigParser) -> None
    """Remove diurnal shaping maps so eps mode paces a flat rate. Replay
    stanzas are left untouched (rule 6); their pacing is engine-driven."""
    for key in list(parser.defaults().keys()):
        if key in RATE_MAP_KEYS:
            del parser.defaults()[key]
    for section in parser.sections():
        if _is_replay(parser, section):
            continue
        for key in RATE_MAP_KEYS:
            parser.remove_option(section, key)


def declared_eps_weights(parser, sections):
    # type: (configparser.RawConfigParser, List[str]) -> List[float]
    """Per-stanza EPS estimates from declared count/interval.

    Stanzas without a usable declaration take the mean of the declared
    estimates (equal split when nothing is declared), per the contract's
    "proportionally to declared estimates, equally when undeclared".
    """
    raw = []  # type: List[Optional[float]]
    for section in sections:
        count = _get_float(parser, section, "count")
        interval = _get_float(parser, section, "interval")
        if count is not None and count > 0:
            step = interval if interval and interval > 0 \
                else EVENTGEN_DEFAULT_INTERVAL_S
            raw.append(count / step)
        else:
            raw.append(None)
    declared = [w for w in raw if w is not None]
    fill = (sum(declared) / len(declared)) if declared else 1.0
    return [w if w is not None else fill for w in raw]


def _get_float(parser, section, option):
    # type: (configparser.RawConfigParser, str, str) -> Optional[float]
    value = parser.get(section, option, fallback=None)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _fmt_number(value):
    # type: (float) -> str
    if value == int(value):
        return str(int(value))
    return ("%.6f" % value).rstrip("0").rstrip(".")


def rewrite(parser, rate_mode, share_value, overdrive, sample_dir,
            slot=0, total_workers=1, weights=None):
    # type: (configparser.RawConfigParser, str, Optional[float], float, str, int, int, Optional[Sequence[float]]) -> configparser.RawConfigParser
    """Apply the contract's rewrite rules in place and return the parser."""
    _strip_output_keys(parser)

    for section in parser.sections():
        parser.set(section, "outputMode", "stoker")
        parser.set(section, "sampleDir", sample_dir)

    paced = [s for s in sample_sections(parser) if not _is_replay(parser, s)]

    if rate_mode == "eps":
        if share_value is None or share_value <= 0:
            raise ConfRewriteError("eps mode requires share_value > 0")
        # eps is a flat instantaneous rate: strip shaping maps so the engine
        # supplies a steady stream the token bucket paces to the exact share.
        _strip_rate_maps(parser)
        if paced:
            _rewrite_eps(parser, paced, share_value, overdrive, weights)
    elif rate_mode == "per_day_gb":
        if share_value is None or share_value <= 0:
            raise ConfRewriteError("per_day_gb mode requires share_value > 0")
        if paced:
            _rewrite_per_day_gb(parser, paced, share_value, overdrive)
    elif rate_mode == "count_interval":
        _rewrite_count_interval(parser, paced, slot, total_workers)
    else:
        raise ConfRewriteError("unknown rate mode %r" % rate_mode)
    return parser


def _rewrite_eps(parser, sections, share_eps, overdrive, weights):
    # type: (configparser.RawConfigParser, List[str], float, float, Optional[Sequence[float]]) -> None
    if weights is None:
        weights = declared_eps_weights(parser, sections)
    if len(weights) != len(sections):
        raise ConfRewriteError("weights length %d != stanza count %d"
                               % (len(weights), len(sections)))
    total = int(round(share_eps * overdrive))
    counts = largest_remainder(total, weights)
    for section, count in zip(sections, counts):
        parser.set(section, "interval", "1")
        parser.set(section, "count", str(max(1, count)))
        parser.remove_option(section, "randomizeCount")


def _rewrite_per_day_gb(parser, sections, share_gb, overdrive):
    # type: (configparser.RawConfigParser, List[str], float, float) -> None
    target = share_gb * overdrive
    declared = {}  # type: Dict[str, float]
    for section in sections:
        vol = _get_float(parser, section, "perDayVolume")
        if vol is not None and vol > 0:
            declared[section] = vol
    undeclared = [s for s in sections if s not in declared]
    # Undeclared stanzas take the equal-split remainder (target/n each);
    # declared stanzas share the rest proportionally to their volumes.
    per_undeclared = target / len(sections) if undeclared else 0.0
    declared_target = max(0.0, target - per_undeclared * len(undeclared))
    declared_sum = sum(declared.values())
    for section in sections:
        if section in declared:
            share = declared_target * declared[section] / declared_sum \
                if declared_sum > 0 else 0.0
        else:
            share = per_undeclared
        parser.set(section, "perDayVolume", _fmt_number(share))


def _rewrite_count_interval(parser, sections, slot, total_workers):
    # type: (configparser.RawConfigParser, List[str], int, int) -> None
    if not 0 <= slot < total_workers:
        raise ConfRewriteError("slot %d out of range for %d workers"
                               % (slot, total_workers))
    for section in sections:
        count = _get_float(parser, section, "count")
        if count is None or count < 0:
            continue  # interval and everything else untouched
        shares = largest_remainder(int(count), [1.0] * total_workers)
        parser.set(section, "count", str(shares[slot]))


def rewrite_file(src, dst, rate_mode, share_value, overdrive, sample_dir,
                 slot=0, total_workers=1, weights=None):
    # type: (str, str, str, Optional[float], float, str, int, int, Optional[Sequence[float]]) -> str
    """Load src, rewrite, write the private copy to dst. Returns dst."""
    parser = load_conf(src)
    rewrite(parser, rate_mode, share_value, overdrive, sample_dir,
            slot=slot, total_workers=total_workers, weights=weights)
    write_conf(parser, dst)
    return dst
