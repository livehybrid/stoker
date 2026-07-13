# -*- coding: utf-8 -*-
"""Unit tests for the metrics engine's pattern library + value sampler."""
from __future__ import absolute_import

import os
import random
import sys

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_METRICS_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "engines", "metrics")
if _METRICS_DIR not in sys.path:
    sys.path.insert(0, _METRICS_DIR)

from stoker_metrics import patterns as p  # noqa: E402


# ---- activity shapes ----

def test_constant_is_flat():
    for h in range(24):
        assert p.activity({"type": "constant"}, h) == 1.0
    assert p.activity({"type": "constant", "level": 0.3}, 12) == pytest.approx(0.3)


def test_sine_peaks_at_peak_hour():
    pat = {"type": "sine", "peak_h": 14, "period_h": 24}
    peak = p.activity(pat, 14)
    assert peak == pytest.approx(1.0, abs=1e-6)
    # Anti-peak (12 h away) is the trough.
    assert p.activity(pat, 2) < 0.05
    # Rising through the morning, near peak by mid-afternoon.
    assert p.activity(pat, 8) < p.activity(pat, 13)


def test_business_double_hump_has_two_peaks_and_a_lunch_dip():
    pat = {"type": "business_double_hump", "morning_peak_h": 10,
           "afternoon_peak_h": 15, "width_h": 1.6}
    overnight = p.activity(pat, 3)
    morning = p.activity(pat, 10)
    lunch = p.activity(pat, 12.5)
    afternoon = p.activity(pat, 15)
    evening = p.activity(pat, 21)
    assert morning > 0.7 and afternoon > 0.6          # two humps
    assert lunch < morning and lunch < afternoon      # dip between them
    assert overnight < 0.15 and evening < 0.2         # quiet outside business hours


def test_business_hours_plateau_and_quiet_overnight():
    pat = {"type": "business_hours", "start_h": 8, "end_h": 18, "ramp_h": 1}
    assert p.activity(pat, 3) < 0.1                    # overnight ~ baseline
    assert p.activity(pat, 12) == pytest.approx(1.0)   # midday plateau
    # Ramp is [start-ramp, start] = [7, 8]; sample within it.
    assert p.activity(pat, 7.2) < p.activity(pat, 7.8) < p.activity(pat, 9)


def test_ramp_is_monotonic_across_the_day():
    pat = {"type": "ramp", "from": 0.1, "to": 1.0}
    vals = [p.activity(pat, h) for h in range(24)]
    assert vals == sorted(vals)
    assert vals[0] < vals[-1]


def test_spike_is_baseline_except_at_scheduled_hours():
    pat = {"type": "spike", "baseline": 0.1, "amplitude": 0.9,
           "width_h": 0.25, "spikes_h": [3, 15]}
    assert p.activity(pat, 3) > 0.8                    # spike
    assert p.activity(pat, 9) == pytest.approx(0.1, abs=0.02)  # baseline


def test_random_walk_is_bounded_and_stateful():
    rng = random.Random(7)
    state = {"rng": rng, "rw": 0.5}
    vals = [p.activity({"type": "random_walk", "step": 0.1}, h % 24, state=state)
            for h in range(200)]
    assert all(0.0 <= v <= 1.0 for v in vals)
    # It actually moves (not frozen).
    assert len(set(round(v, 3) for v in vals)) > 10


def test_activity_always_in_unit_interval():
    for ptype in p.PATTERN_TYPES:
        for h in [0, 6, 10, 12, 15, 18, 23.5]:
            a = p.activity({"type": ptype}, h, state={"rng": random.Random(1)})
            assert 0.0 <= a <= 1.0, (ptype, h, a)


# ---- value sampler ----

def test_sample_respects_min_max_envelope():
    rng = random.Random(3)
    vals = [p.sample_value(1.0, 5, 800, 1500, "gauge", 0.6, rng) for _ in range(5000)]
    assert min(vals) >= 5 and max(vals) <= 1500


def test_center_tracks_activity_between_min_and_p95():
    # No noise: value == min at a=0, == p95 at a=1.
    rng = random.Random(0)
    assert p.sample_value(0.0, 10, 200, 500, "gauge", 0.0, rng) == pytest.approx(10)
    assert p.sample_value(1.0, 10, 200, 500, "gauge", 0.0, rng) == pytest.approx(200)
    assert p.sample_value(0.5, 10, 200, 500, "gauge", 0.0, rng) == pytest.approx(105)


def test_count_kind_is_non_negative_integer():
    rng = random.Random(1)
    for _ in range(500):
        v = p.sample_value(0.7, 5, 800, 1500, "count", 0.2, rng)
        assert v == int(v) and v >= 0


def test_counter_kind_is_monotonic_cumulative():
    rng = random.Random(1)
    state = {"total": 0}
    prev = -1.0
    for _ in range(100):
        v = p.sample_value(0.5, 0, 40, 90, "counter", 0.1, rng, state=state)
        assert v >= prev                      # never decreases
        assert v == int(v)
        prev = v
    assert prev > 0                            # actually accumulated


def test_sampler_is_deterministic_for_a_fixed_seed():
    a = [p.sample_value(0.6, 5, 800, 1500, "count", 0.2, random.Random(99))
         for _ in range(1)]
    b = [p.sample_value(0.6, 5, 800, 1500, "count", 0.2, random.Random(99))
         for _ in range(1)]
    assert a == b
