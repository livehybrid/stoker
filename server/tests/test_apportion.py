"""Apportionment + ceiling tests.

The control plane apportions a run's rate across worker slots by largest
remainder; the integer parts must sum to *exactly* the total (the ±1 % pacing
guarantee downstream depends on the split being honest). Ceilings reject a
per-worker share above the engine table and suggest the smallest fleet size that
would fit. Both are pure functions (no DB), so these are fast and deterministic.
"""

from __future__ import annotations

import math

import pytest

from server.engines.apportion import (
    apportion_shares,
    largest_remainder,
)
from server.engines.ceilings import (
    CEILINGS,
    check_slice,
    eps_to_gb_day,
    gb_day_to_eps,
)


# --------------------------------------------------------------------------- #
# largest_remainder: sums exactly, length preserved, non-negative.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("total", [0, 1, 2, 7, 100, 1000, 1543, 99999])
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 16])
def test_largest_remainder_sums_exactly_equal_weights(total, n):
    parts = largest_remainder(total, [1.0] * n)
    assert len(parts) == n
    assert sum(parts) == total
    assert all(isinstance(p, int) and p >= 0 for p in parts)
    # Equal weights: parts differ by at most 1 (balanced split).
    assert max(parts) - min(parts) <= 1


def test_largest_remainder_weighted_split_is_proportional():
    # 100 across weights 1:2:1 -> 25 / 50 / 25.
    parts = largest_remainder(100, [1.0, 2.0, 1.0])
    assert parts == [25, 50, 25]
    assert sum(parts) == 100


def test_largest_remainder_ties_resolve_to_lower_index():
    # 10 across 4 equal weights -> 2.5 each; the two extra units go to the two
    # lowest indices (stable tie-break on the fractional part).
    parts = largest_remainder(10, [1.0, 1.0, 1.0, 1.0])
    assert parts == [3, 3, 2, 2]
    assert sum(parts) == 10


def test_largest_remainder_zero_weights_fall_back_to_equal():
    parts = largest_remainder(9, [0.0, 0.0, 0.0])
    assert sum(parts) == 9
    assert parts == [3, 3, 3]


def test_largest_remainder_empty_weights():
    assert largest_remainder(5, []) == []


def test_largest_remainder_negative_total_raises():
    with pytest.raises(ValueError):
        largest_remainder(-1, [1.0, 1.0])


# --------------------------------------------------------------------------- #
# apportion_shares: one key per mode, exact sums.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("workers", [1, 2, 3, 4, 7])
def test_apportion_eps_sums_exactly(workers):
    shares = apportion_shares("eps", 1000.0, workers)
    assert len(shares) == workers
    assert all(set(s.keys()) == {"eps"} for s in shares)
    assert math.fsum(s["eps"] for s in shares) == pytest.approx(1000.0, abs=1e-9)


@pytest.mark.parametrize("workers", [1, 3, 8])
def test_apportion_per_day_gb_sums_exactly(workers):
    shares = apportion_shares("per_day_gb", 24.0, workers)
    assert all(set(s.keys()) == {"per_day_gb"} for s in shares)
    assert math.fsum(s["per_day_gb"] for s in shares) == pytest.approx(24.0, abs=1e-9)


def test_apportion_eps_equal_split_value():
    shares = apportion_shares("eps", 1000.0, 4)
    assert all(s["eps"] == pytest.approx(250.0) for s in shares)


def test_apportion_count_interval_integer_split_sums_exactly():
    shares = apportion_shares("count_interval", 100, 3)
    counts = [int(s["count"]) for s in shares]
    assert sum(counts) == 100
    assert counts == [34, 33, 33]
    assert all(set(s.keys()) == {"count"} for s in shares)


def test_apportion_count_interval_none_yields_zero_counts():
    shares = apportion_shares("count_interval", None, 3)
    assert shares == [{"count": 0.0}, {"count": 0.0}, {"count": 0.0}]


def test_apportion_weighted_eps_split():
    shares = apportion_shares("eps", 900.0, 3, weights=[1.0, 2.0, 3.0])
    values = [s["eps"] for s in shares]
    assert values[0] == pytest.approx(150.0)
    assert values[1] == pytest.approx(300.0)
    assert values[2] == pytest.approx(450.0)
    assert math.fsum(values) == pytest.approx(900.0, abs=1e-9)


@pytest.mark.parametrize("mode", ["eps", "per_day_gb"])
def test_apportion_rate_modes_require_positive_value(mode):
    with pytest.raises(ValueError):
        apportion_shares(mode, None, 2)
    with pytest.raises(ValueError):
        apportion_shares(mode, 0.0, 2)


def test_apportion_zero_workers_raises():
    with pytest.raises(ValueError):
        apportion_shares("eps", 100.0, 0)


def test_apportion_weights_length_mismatch_raises():
    with pytest.raises(ValueError):
        apportion_shares("eps", 100.0, 3, weights=[1.0, 1.0])


def test_apportion_unknown_mode_raises():
    with pytest.raises(ValueError):
        apportion_shares("nonsense", 100.0, 2)


# --------------------------------------------------------------------------- #
# Ceilings: within limit ok; over limit rejected + suggested_workers.
# --------------------------------------------------------------------------- #

def test_ceiling_ok_under_eps_limit():
    result = check_slice("eps", 1000.0, bytes_per_event=120)
    assert result.ok
    assert result.suggested_workers is None


def test_ceiling_rejects_over_eps_with_suggestion():
    # eventgen ceiling is 5000 EPS; 20000 needs at least ceil(20000/5000)=4.
    result = check_slice("eps", 20000.0, bytes_per_event=None)
    assert not result.ok
    assert result.limiting_factor == "eps"
    assert result.suggested_workers == 4
    # The suggested fleet size actually brings the per-worker share under the
    # ceiling (that is what the operator will use to rerun).
    per_worker = 20000.0 / result.suggested_workers
    assert per_worker <= CEILINGS["eventgen"]["max_eps_per_worker"]


def test_ceiling_rejects_over_gb_day_with_suggestion():
    # 25 GB/day per-worker ceiling; 100 GB/day needs at least 4.
    result = check_slice("per_day_gb", 100.0, bytes_per_event=None)
    assert not result.ok
    assert result.limiting_factor == "gb_day"
    assert result.suggested_workers == 4
    assert 100.0 / result.suggested_workers <= CEILINGS["eventgen"]["max_gb_day_per_worker"]


def test_ceiling_eps_derived_gb_day_binds_first():
    # A modest EPS with a huge bytes/event blows the GB/day ceiling before the
    # EPS ceiling: whichever binds first is reported.
    # 1000 EPS * 40000 B/event -> 40 GB/day (> 25 ceiling), EPS 1000 (< 5000).
    result = check_slice("eps", 1000.0, bytes_per_event=40000)
    assert not result.ok
    assert result.limiting_factor == "gb_day"
    assert result.suggested_workers is not None and result.suggested_workers >= 2


def test_ceiling_suggested_is_at_least_two_when_over_at_one():
    # Just over the EPS ceiling at a single worker -> at least 2 workers.
    result = check_slice("eps", 5001.0, bytes_per_event=None)
    assert not result.ok
    assert result.suggested_workers == 2


def test_ceiling_count_interval_always_ok():
    assert check_slice("count_interval", None).ok
    assert check_slice("count_interval", 999999.0).ok


def test_ceiling_unknown_engine_passes():
    # No ceiling table for an unknown engine -> conservative pass, documented.
    result = check_slice("eps", 999999.0, engine="myengine")
    assert result.ok


def test_ceiling_zero_or_none_share_passes():
    assert check_slice("eps", None).ok
    assert check_slice("eps", 0.0).ok


# --------------------------------------------------------------------------- #
# Conversion helpers (used by the ceiling + estimate paths).
# --------------------------------------------------------------------------- #

def test_gb_day_eps_round_trip():
    # 25 GB/day at 250 B/event -> EPS; back to GB/day is the same.
    eps = gb_day_to_eps(25.0, 250.0)
    assert eps is not None
    back = eps_to_gb_day(eps, 250.0)
    assert back == pytest.approx(25.0)


def test_conversion_unknown_bytes_per_event_is_none():
    assert gb_day_to_eps(25.0, None) is None
    assert gb_day_to_eps(25.0, 0) is None
    assert eps_to_gb_day(1000.0, None) is None
