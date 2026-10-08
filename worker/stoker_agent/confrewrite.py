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
from typing import Dict, List, Optional, Sequence, Tuple

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
            slot=0, total_workers=1, weights=None, backfill_window_s=None,
            backfill_end_offset_s=None, backfill_mode="window",
            backfill_density_eps=None):
    # type: (configparser.RawConfigParser, str, Optional[float], float, str, int, int, Optional[Sequence[float]], Optional[float], Optional[float], str, Optional[float]) -> configparser.RawConfigParser
    """Apply the contract's rewrite rules in place and return the parser.

    ``backfill_window_s`` (when set) turns this into an eventgen **backfill**
    run over a historical range. ``backfill_end_offset_s`` (seconds back from now
    that the window ENDS, 0 or None meaning "up to now") is what allows an
    arbitrary range such as January to April rather than only "the last N". Both
    bounds are written as offsets back from now, which both engines parse the
    same way, and the event text timestamp and the HEC ``_time`` agree either way.

    ``backfill_mode`` picks how the history is produced, because the two engines
    can do very different things with it:

    ``"sweep"`` (firebox)
        The engine's own ordered backfill: it walks the range from the start,
        one interval at a time, each with its own timestamp window. The window
        therefore FILLS, in order, and a run that is cut short leaves a shorter
        complete history. ``backfill_density_eps`` (fleet-wide eps) sets the
        count/interval, so the history is exactly as dense as the spec asks
        while the agent's token bucket still paces delivery.

    ``"window"`` (the vendored Python eventgen)
        Its native ``backfill`` rater is non-functional in that tree, so
        instead: widen each paced stanza's timestamp window across the range, so
        every event gets a uniformly random historical time. Density is then a
        by-product of ``duration x delivery rate``, which is why a part-finished
        run reads as a sparse smear across the range rather than a filled
        window. Kept as the fallback because it is all that engine can do.

    Neither mode reproduces the diurnal shape across the window (metrics
    backfill does that).
    """
    _strip_output_keys(parser)

    for section in parser.sections():
        parser.set(section, "outputMode", "stoker")
        parser.set(section, "sampleDir", sample_dir)

    paced = [s for s in sample_sections(parser) if not _is_replay(parser, s)]

    # Read while the stanzas still carry their DECLARED count/interval: the rate
    # rewrite below replaces both with this worker's delivery share, and a sweep
    # apportions the historical density by the pack's own proportions, not by
    # numbers the rewrite has already put there.
    sweep_weights = None  # type: Optional[Sequence[float]]
    if backfill_mode == "sweep" and backfill_window_s and backfill_window_s > 0:
        sweep_weights = weights if weights is not None \
            else declared_eps_weights(parser, paced)

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

    if backfill_window_s and backfill_window_s > 0:
        # Both bounds are expressed as offsets BACK from now, which is what lets
        # an arbitrary historical range work ("January to April") rather than
        # only "the last N". `latest` was pinned to `now`, so every window ended
        # at the present whatever range was asked for. Splunk relative time
        # handles a negative latest, and the engines parse it the same way.
        back_to_start = int(backfill_window_s)
        back_to_end = 0
        if backfill_end_offset_s and backfill_end_offset_s > 0:
            back_to_end = int(backfill_end_offset_s)
            back_to_start = back_to_end + int(backfill_window_s)
        if backfill_mode == "sweep":
            _rewrite_backfill_sweep(parser, paced, back_to_start, back_to_end,
                                    backfill_density_eps, sweep_weights)
        else:
            for section in paced:
                parser.set(section, "earliest", "-%ds" % back_to_start)
                parser.set(section, "latest",
                           "now" if back_to_end == 0 else "-%ds" % back_to_end)

    return parser


def _rewrite_backfill_sweep(parser, sections, back_to_start, back_to_end,
                            density_eps, weights):
    # type: (configparser.RawConfigParser, List[str], int, int, Optional[float], Optional[Sequence[float]]) -> None
    """Hand the range to firebox's ordered sweep instead of widening the window.

    Two things are being set, and they are deliberately different numbers:

    * ``backfill`` / ``backfillEnd`` bound the range, and ``backfillOnly`` stops
      the engine when it reaches the end instead of carrying on live. Without
      that last one a January-to-April backfill would start stamping
      present-time events the moment it caught up.
    * ``count`` / ``interval`` are the historical DENSITY, not the delivery
      rate. Delivery is still paced by the agent's token bucket, so the sweep
      advances at whatever the target accepts while the history it writes stays
      exactly as dense as the spec asks. Under the widening these were the
      delivery rate and density was left to fall out of the run's duration,
      which is why the arithmetic used to cancel.

    No overdrive is applied for the same reason: overdriving the engine exists
    to stop the bucket starving on a live run, and here it would simply write a
    denser history than was asked for.
    """
    for section in sections:
        parser.set(section, "backfill", "-%ds" % back_to_start)
        if back_to_end > 0:
            parser.set(section, "backfillEnd", "-%ds" % back_to_end)
        else:
            parser.remove_option(section, "backfillEnd")
        parser.set(section, "backfillOnly", "true")
        # The sweep supplies each interval's own window, so earliest/latest are
        # never read. Set them to the range anyway: a human reading the
        # rewritten conf should not have to know that to see what it covers.
        parser.set(section, "earliest", "-%ds" % back_to_start)
        parser.set(section, "latest",
                   "now" if back_to_end == 0 else "-%ds" % back_to_end)
    if density_eps is None or density_eps <= 0:
        # Nothing said how dense the history should be (a spec with no eps of
        # its own). Leave the paced counts as the eps rewrite set them, which is
        # the delivery rate: the previous behaviour for those modes.
        return
    if weights is None:
        weights = declared_eps_weights(parser, sections)
    if len(weights) != len(sections):
        raise ConfRewriteError("weights length %d != stanza count %d"
                               % (len(weights), len(sections)))
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ConfRewriteError("backfill density needs a positive stanza weight")
    for section, weight in zip(sections, weights):
        eps = density_eps * weight / total_weight
        if eps <= 0:
            # This stanza contributes no history. `end = 0` is how eventgen says
            # "generate nothing", and is honest in a way count = 0 is not.
            parser.set(section, "end", "0")
            continue
        interval, count = _density_count_interval(eps)
        parser.set(section, "interval", str(interval))
        parser.set(section, "count", str(count))
        parser.remove_option(section, "randomizeCount")


# A one-second interval can only express a whole number of events per second, so
# a stanza at 1.4 eps would round to 1 and write a history 29 % thinner than
# asked. Stretching the interval until the count is at least this big keeps the
# rounding error under about 5 %, and the wider window is the natural spacing for
# that rate anyway (events land uniformly inside their own interval).
DENSITY_MIN_COUNT = 10
# ...but not indefinitely: an interval longer than this coarsens the sweep's
# ordering for no gain, and below roughly one event per five minutes per stanza
# no integer count/interval pair represents the rate at all.
DENSITY_MAX_INTERVAL_S = 3600


def _density_count_interval(eps):
    # type: (float) -> Tuple[int, int]
    """``(interval, count)`` whose ratio is as close to ``eps`` as integers get."""
    if eps >= DENSITY_MIN_COUNT:
        return 1, max(1, int(round(eps)))
    interval = min(DENSITY_MAX_INTERVAL_S,
                   int(math.ceil(DENSITY_MIN_COUNT / eps)))
    return interval, max(1, int(round(eps * interval)))


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


def assigned_stanza_count(parser, rate_mode):
    # type: (configparser.RawConfigParser, str) -> int
    """How many stanzas of an already-REWRITTEN conf will actually emit.

    This is the worker's own answer to "does this slot hold any work?", used by
    the agent's heartbeat so an idle worker can explain itself to the control
    plane instead of showing a healthy lease at 0 EPS forever.

    * Gated modes (``eps`` / ``per_day_gb``): every paced stanza emits (the
      rewrite forces ``count >= 1`` / a positive ``perDayVolume`` share).
    * ``count_interval``: the rewrite split each stanza's declared ``count``
      across the fleet by largest remainder, so a stanza whose rewritten count
      is 0 emits nothing on this worker. A stanza that declares no ``count``
      is untouched by the rewrite and emits its engine-default volume, so it
      always counts as assigned.
    * ``mode = replay`` stanzas always emit (engine-paced, never split) and
      count as assigned regardless of mode.
    * ``end = 0`` is eventgen for "generate nothing", which a backfill sweep
      writes for a stanza that holds no share of the history. It emits in no
      mode, so it is never assigned.
    """
    replay = [s for s in sample_sections(parser) if _is_replay(parser, s)]
    paced = [s for s in sample_sections(parser) if not _is_replay(parser, s)]
    assigned = len(replay)
    for section in paced:
        if (parser.get(section, "end", fallback="") or "").strip() == "0":
            continue
        if rate_mode != "count_interval":
            assigned += 1
            continue
        count = _get_float(parser, section, "count")
        # A negative count ("the whole sample every interval") is left unsplit
        # by _rewrite_count_interval, so this slot does generate it; only a
        # count of exactly 0 after the split means this slot has nothing to do.
        if count is None or count > 0 or count < 0:
            assigned += 1
    return assigned


def rewrite_file(src, dst, rate_mode, share_value, overdrive, sample_dir,
                 slot=0, total_workers=1, weights=None, backfill_window_s=None,
                 backfill_end_offset_s=None, backfill_mode="window",
                 backfill_density_eps=None):
    # type: (str, str, str, Optional[float], float, str, int, int, Optional[Sequence[float]], Optional[float], Optional[float], str, Optional[float]) -> str
    """Load src, rewrite, write the private copy to dst. Returns dst."""
    parser = load_conf(src)
    rewrite(parser, rate_mode, share_value, overdrive, sample_dir,
            slot=slot, total_workers=total_workers, weights=weights,
            backfill_window_s=backfill_window_s,
            backfill_end_offset_s=backfill_end_offset_s,
            backfill_mode=backfill_mode,
            backfill_density_eps=backfill_density_eps)
    write_conf(parser, dst)
    return dst
