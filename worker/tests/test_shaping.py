"""rate_shape=pack: the agent's bucket follows the pack's time-of-day maps."""
import calendar
import time

from stoker_agent import confrewrite, shaping


def _parser(text):
    p = confrewrite.make_parser()
    p.read_string(text)
    return p


HOURS = {str(h): (2.0 if 9 <= h < 17 else 0.5) for h in range(24)}


def _local_epoch(year, month, day, hour, minute=0):
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def test_curve_mean_peak_and_factor():
    import json
    p = _parser("[web.sample]\ncount = 10\nhourOfDayRate = %s\n" % json.dumps(HOURS))
    curve = shaping.curve_from_parser(p)
    assert curve is not None
    # 8 busy hours at 2.0, 16 quiet at 0.5 -> mean 1.0, peak 2.0
    assert abs(curve.mean - 1.0) < 1e-9 and curve.peak == 2.0
    assert curve.peak_ratio == 2.0
    assert curve.factor(_local_epoch(2026, 9, 28, 10)) == 2.0
    assert curve.factor(_local_epoch(2026, 9, 28, 3)) == 0.5
    assert abs(curve.rate(1000.0, _local_epoch(2026, 9, 28, 10)) - 2000.0) < 1e-6
    # averaged over a day the shaped rate is the share
    day = [curve.rate(1000.0, _local_epoch(2026, 9, 28, h, 30)) for h in range(24)]
    assert abs(sum(day) / 24.0 - 1000.0) < 1e-6


def test_weekday_numbering_matches_eventgen_sunday_zero():
    p = _parser('[global]\ndayOfWeekRate = {"0": 0.2, "6": 3.0}\n[a.sample]\ncount = 1\n')
    curve = shaping.curve_from_parser(p)
    sunday = _local_epoch(2026, 9, 27, 12)      # 2026-09-27 is a Sunday
    saturday = _local_epoch(2026, 9, 26, 12)
    monday = _local_epoch(2026, 9, 28, 12)
    assert curve.factor(sunday) == 0.2
    assert curve.factor(saturday) == 3.0
    assert curve.factor(monday) == 1.0          # missing key leaves the factor alone


def test_no_maps_or_all_zero_means_no_curve():
    assert shaping.curve_from_parser(_parser("[a.sample]\ncount = 1\n")) is None
    zero = {str(h): 0 for h in range(24)}
    import json
    assert shaping.curve_from_parser(_parser("[a.sample]\nhourOfDayRate = %s\n" % json.dumps(zero))) is None


def test_replay_stanza_maps_are_ignored_and_stanza_beats_global():
    p = _parser('[global]\nhourOfDayRate = {"0": 5}\n'
                '[r.sample]\nmode = replay\nhourOfDayRate = {"0": 9}\n'
                '[a.sample]\nhourOfDayRate = {"0": 3}\n')
    curve = shaping.curve_from_parser(p)
    assert curve.maps["hourOfDayRate"] == {0: 3.0}


def test_rate_never_reaches_zero():
    import json
    hours = {str(h): (0 if h == 3 else 1) for h in range(24)}
    curve = shaping.curve_from_parser(_parser("[a.sample]\nhourOfDayRate = %s\n" % json.dumps(hours)))
    assert curve.rate(100.0, _local_epoch(2026, 9, 28, 3)) == shaping.MIN_RATE
