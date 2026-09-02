"""Submit-time sharding guard (``server.engines.sharding``).

The metrics stride shard (``series[slot::total_workers]``) and the eventgen
``count_interval`` largest-remainder split both starve surplus workers when
the fleet is larger than the discrete work: the extra slots claim, heartbeat
healthily and emit nothing for the whole run. ``check_sharding`` predicts that
at submit time so the route can refuse with an actionable 422 — these tests
pin the prediction to the exact split the worker performs (same
``largest_remainder``) and the pass-through behaviour for everything else
(gated modes, rawreplay, single worker, unreadable confs).
"""

from __future__ import annotations

import os

from server.engines import ShardingCheck, check_sharding  # package exports
from server.engines.sharding import (
    count_interval_active_workers,
    eventgen_stanza_counts,
    metrics_active_workers,
)


def _metrics_config(*value_counts):
    # type: (*int) -> dict
    """A metricgen config whose series matrix is the product of value_counts."""
    dims = [
        {"key": "d%d" % i, "values": ["v%d" % v for v in range(n)]}
        for i, n in enumerate(value_counts)
    ]
    return {"resolution_s": 10, "dimensions": dims,
            "metrics": [{"name": "m", "kind": "gauge", "min": 0, "p95": 1, "max": 2}]}


def _write_conf(tmp_path, body):
    pack_dir = tmp_path / "pack"
    (pack_dir / "default").mkdir(parents=True)
    (pack_dir / "default" / "eventgen.conf").write_text(body, encoding="utf-8")
    return str(pack_dir)


# --------------------------------------------------------------------------- #
# metrics: the series stride shard.
# --------------------------------------------------------------------------- #

def test_metrics_over_provisioned_is_refused_with_suggestion():
    # The reported bug: 4 series across 10 workers leaves slots 4-9 with nothing.
    check = check_sharding("metrics", "count_interval", 10,
                           metrics_config=_metrics_config(2, 2))
    assert isinstance(check, ShardingCheck)
    assert check.ok is False
    assert check.active_workers == 4
    assert check.total_workers == 10
    assert check.suggested_workers == 4
    assert check.limiting_factor == "metrics_series"
    # The operator-facing detail names the idle slot span and the fix.
    assert "4-9" in check.detail
    assert "use at most 4 workers" in check.detail


def test_metrics_exact_fit_and_underprovision_pass():
    assert check_sharding("metrics", "count_interval", 4,
                          metrics_config=_metrics_config(2, 2)).ok
    assert check_sharding("metrics", "count_interval", 3,
                          metrics_config=_metrics_config(2, 2)).ok


def test_metrics_no_dimensions_is_one_series():
    # No dimensions = a single unlabelled series: only 1 worker can hold work.
    check = check_sharding("metrics", "count_interval", 2,
                           metrics_config=_metrics_config())
    assert not check.ok
    assert check.active_workers == 1
    assert check.suggested_workers == 1


def test_metrics_pack_detected_by_config_even_without_engine():
    # The route passes builder_config_json whenever the pack carries one; the
    # spec engine string is metrics by the route's own 1c gate, but the guard
    # keys off the config too (mirrors the route's dispatch).
    check = check_sharding("eventgen", "count_interval", 10,
                           metrics_config=_metrics_config(2, 2))
    assert not check.ok
    assert check.limiting_factor == "metrics_series"


def test_single_worker_always_passes():
    assert check_sharding("metrics", "count_interval", 1,
                          metrics_config=_metrics_config(2, 2)).ok
    assert check_sharding("eventgen", "count_interval", 1).ok


def test_metrics_active_workers_matches_stride():
    # Ground truth: len(series[slot::total]) > 0 for exactly min(series, total)
    # slots.
    for series in (1, 3, 4, 7):
        for workers in (1, 2, 4, 10):
            stride_active = sum(
                1 for slot in range(workers)
                if len(range(slot, series, workers)) > 0)
            assert metrics_active_workers(series, workers) == stride_active


# --------------------------------------------------------------------------- #
# eventgen count_interval: the per-stanza count split.
# --------------------------------------------------------------------------- #

def test_count_interval_over_provisioned_is_refused(tmp_path):
    # The reported bug: count=6 across 10 workers -> [1x6, 0x4]; slots 6-9 idle.
    pack_dir = _write_conf(tmp_path, "[web]\nmode = sample\ncount = 6\ninterval = 10\n")
    check = check_sharding("eventgen", "count_interval", 10, pack_dir=pack_dir)
    assert not check.ok
    assert check.active_workers == 6
    assert check.suggested_workers == 6
    assert check.limiting_factor == "count_interval"
    assert "6-9" in check.detail
    assert "use at most 6 workers" in check.detail


def test_count_interval_union_across_stanzas(tmp_path):
    # Two stanzas (counts 2 and 3) across 4 workers: slots 0-2 get work from
    # the count=3 stanza, slot 3 from neither -> 3 active.
    pack_dir = _write_conf(
        tmp_path,
        "[a]\nmode = sample\ncount = 2\n\n[b]\nmode = sample\ncount = 3\n")
    check = check_sharding("eventgen", "count_interval", 4, pack_dir=pack_dir)
    assert not check.ok
    assert check.active_workers == 3
    assert check.suggested_workers == 3


def test_count_interval_enough_count_passes(tmp_path):
    pack_dir = _write_conf(tmp_path, "[web]\nmode = sample\ncount = 100\n")
    assert check_sharding("eventgen", "count_interval", 10, pack_dir=pack_dir).ok


def test_count_interval_undeclared_count_passes(tmp_path):
    # A stanza with no count is untouched by the worker's rewrite and emits its
    # engine-default volume on every worker: nobody starves, so no block.
    pack_dir = _write_conf(
        tmp_path, "[a]\nmode = sample\ncount = 2\n\n[b]\nmode = sample\n")
    assert check_sharding("eventgen", "count_interval", 10, pack_dir=pack_dir).ok


def test_count_interval_unreadable_conf_passes_conservatively(tmp_path):
    # The guard must never block a submit on a guess: lint owns conf validity.
    check = check_sharding("eventgen", "count_interval", 10,
                           pack_dir=str(tmp_path / "missing"))
    assert check.ok
    assert check_sharding("eventgen", "count_interval", 10, pack_dir=None).ok


def test_gated_modes_and_rawreplay_pass_through(tmp_path):
    # eps / per_day_gb split a continuous rate: every slot gets a share.
    pack_dir = _write_conf(tmp_path, "[web]\nmode = sample\ncount = 6\n")
    assert check_sharding("eventgen", "eps", 10, pack_dir=pack_dir).ok
    assert check_sharding("eventgen", "per_day_gb", 10, pack_dir=pack_dir).ok
    # rawreplay is single-worker by rule (409 elsewhere); nothing to shard.
    assert check_sharding("rawreplay", "count_interval", 3).ok


def test_replay_stanzas_take_no_count_share(tmp_path):
    # A replay stanza is engine-paced and never split; only the paced stanza's
    # count matters for the split.
    pack_dir = _write_conf(
        tmp_path,
        "[cap]\nmode = replay\ncount = 1\n\n[web]\nmode = sample\ncount = 6\n")
    counts = eventgen_stanza_counts(pack_dir)
    assert counts == [6.0]


def test_eventgen_stanza_counts_reads_conf(tmp_path):
    pack_dir = _write_conf(
        tmp_path,
        "[global]\ncount = 999\n\n[a]\nmode = sample\ncount = 4\n\n"
        "[b]\nmode = sample\n\n[c]\nmode = sample\ncount = nonsense\n")
    counts = eventgen_stanza_counts(pack_dir)
    # global excluded; declared count parsed; missing and unparseable are None.
    assert counts == [4.0, None, None]
    assert eventgen_stanza_counts(os.path.join(str(tmp_path), "nope")) is None


def test_count_interval_active_matches_worker_split():
    # Pin the prediction to the worker's own largest-remainder split.
    from server.engines.apportion import largest_remainder

    for count in (0, 1, 5, 6, 9, 10, 23):
        for workers in (1, 3, 10):
            parts = largest_remainder(count, [1.0] * workers)
            truth = sum(1 for p in parts if p > 0)
            assert count_interval_active_workers([float(count)], workers) == truth
