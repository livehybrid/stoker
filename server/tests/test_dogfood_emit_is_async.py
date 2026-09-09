"""Dogfood telemetry must not be able to slow a run down.

The emit was always failure-isolated: a HEC error is swallowed and a run
carries on. It was not TIME-isolated, and that is the one that hurt. With the
dogfood HEC URL pointed somewhere the control plane could not reach, every run
transition paid the full HTTP timeout before continuing, and a run makes a
dozen transitions on its way through provisioning. The control plane looked
wedged and nothing had failed, so nothing said why.

These pin the property that matters: the caller does not wait.
"""

from __future__ import annotations

import threading
import time

import pytest

from server import metrics_lifecycle

from . import _helpers


@pytest.fixture
def dogfood_settings(settings):
    """Settings with dogfood on, pointed at a URL nothing will actually call."""
    object.__setattr__(settings, "dogfood_hec_url", "https://hec.invalid:8088")
    object.__setattr__(settings, "dogfood_hec_token", "not-a-real-token")
    return settings


@pytest.fixture(autouse=True)
def _drain_between_tests():
    yield
    metrics_lifecycle.flush_emits(timeout=5.0)


def test_a_slow_hec_does_not_delay_the_caller(db_session, make_pack,
                                              dogfood_settings, monkeypatch):
    """The emit happens on another thread, so the transition returns at once."""
    ctx = _helpers.full_run(db_session, make_pack(), dogfood_settings, workers=1)
    run = ctx["run"]

    started = threading.Event()

    def slow_emit(events, settings=None):
        started.set()
        time.sleep(1.5)
        return False

    monkeypatch.setattr(metrics_lifecycle, "emit_hec_events", slow_emit)

    began = time.monotonic()
    metrics_lifecycle.emit_run_transition_event(
        run, "pending", "provisioning", settings=dogfood_settings)
    elapsed = time.monotonic() - began

    # The whole point: the caller is not waiting on the POST.
    assert elapsed < 0.5, "the transition waited for the HEC POST"
    # And the POST really did happen, on some other thread.
    assert started.wait(2.0), "the event was never emitted at all"


def test_the_backlog_is_bounded_rather_than_unbounded(db_session, make_pack,
                                                      dogfood_settings, monkeypatch):
    """A wedged HEC costs capped memory, not growing memory.

    Past the ceiling events are dropped. Dropping telemetry is the right
    outcome; queueing it forever while the endpoint is down is not.
    """
    ctx = _helpers.full_run(db_session, make_pack(), dogfood_settings, workers=1)
    run = ctx["run"]

    release = threading.Event()

    def blocked_emit(events, settings=None):
        release.wait(10.0)
        return False

    monkeypatch.setattr(metrics_lifecycle, "emit_hec_events", blocked_emit)

    try:
        for _ in range(metrics_lifecycle._EMIT_MAX_PENDING + 20):
            metrics_lifecycle.emit_run_transition_event(
                run, "pending", "provisioning", settings=dogfood_settings)
        assert metrics_lifecycle._emit_pending <= metrics_lifecycle._EMIT_MAX_PENDING
        assert metrics_lifecycle._emit_dropped > 0
    finally:
        release.set()
        metrics_lifecycle.flush_emits(timeout=10.0)


def test_emitting_inline_is_still_available_for_callers_that_want_it(
        db_session, make_pack, dogfood_settings, monkeypatch):
    """background=False emits on the calling thread, which is what tests need."""
    ctx = _helpers.full_run(db_session, make_pack(), dogfood_settings, workers=1)
    run = ctx["run"]

    seen = []
    monkeypatch.setattr(
        metrics_lifecycle, "emit_hec_events",
        lambda events, settings=None: seen.append(events) or True)

    metrics_lifecycle.emit_run_transition_event(
        run, "pending", "provisioning", settings=dogfood_settings, background=False)

    assert len(seen) == 1
    assert seen[0][0]["sourcetype"] == "stoker:job"


def test_a_transition_emits_nothing_when_dogfood_is_off(db_session, make_pack,
                                                        settings, monkeypatch):
    """No URL, no token, no thread, no event."""
    ctx = _helpers.full_run(db_session, make_pack(), settings, workers=1)
    run = ctx["run"]

    called = []
    monkeypatch.setattr(
        metrics_lifecycle, "emit_hec_events",
        lambda events, settings=None: called.append(events) or True)

    metrics_lifecycle.emit_run_transition_event(run, "pending", "provisioning",
                                                settings=settings)
    metrics_lifecycle.flush_emits(timeout=2.0)

    assert called == []
