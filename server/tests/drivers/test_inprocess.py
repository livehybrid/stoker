"""InProcessDriver: build gating, env wiring, worker cap, log capture.

The full live path (a spawned agent claiming leases against a running control
plane) belongs to the e2e test; here we verify the driver's construction rules
and the exact env each worker slot receives — plus the capture plumbing with a
guaranteed-fast-failing child (a spawn whose PYTHONPATH cannot reach
``stoker_agent``), so nothing depends on a live server or real workload.
"""

from __future__ import annotations

import dataclasses
import os
import time
from typing import Any
from unittest import mock

import pytest

from server import config as config_mod
from server.drivers.base import DriverError, RunSnapshot
from server.drivers.fake import FakeDriver
from server.drivers.inprocess import (
    InProcessDriver,
    _find_worker_root,
    build_inprocess_driver,
)


def _snapshot(run_id=901, workers=2):
    # type: (int, int) -> RunSnapshot
    env = {
        "STOKER_RUN_ID": str(run_id),
        "STOKER_CONTROL_URL": "https://stoker.example.com",  # public URL: must be overridden
        "STOKER_RUN_JWT": "jwt.header.payload.sig",
        "STOKER_TOTAL_WORKERS": str(workers),
    }
    return RunSnapshot(
        run_id=run_id,
        image="ghcr.io/livehybrid/stoker-worker@sha256:feedface",
        env=env,
        labels={"stoker.run": str(run_id)},
        driver_opts={},
        stop_grace_s=5,
    )


@pytest.fixture()
def inprocess_settings(settings):
    # type: (Any) -> Any
    """The test Settings with the in-process fleet switched on."""
    enabled = dataclasses.replace(settings, inprocess_fleet_enabled=True)
    config_mod.set_settings(enabled)
    yield enabled
    # The parent ``settings`` fixture resets the singleton on teardown.


# --------------------------------------------------------------------------- #
# build_inprocess_driver gating.
# --------------------------------------------------------------------------- #

def test_build_refuses_when_disabled(settings):
    with pytest.raises(DriverError) as excinfo:
        build_inprocess_driver({}, settings=settings)
    assert "STOKER_INPROCESS_FLEET" in str(excinfo.value)


def test_build_refuses_without_worker_source(inprocess_settings):
    with mock.patch("server.drivers.inprocess._find_worker_root",
                    return_value=None):
        with pytest.raises(DriverError) as excinfo:
            build_inprocess_driver({}, settings=inprocess_settings)
    assert "worker source" in str(excinfo.value)


def test_worker_root_discovery_finds_the_repo_layout():
    root = _find_worker_root()
    assert root is not None
    assert os.path.isdir(os.path.join(root, "stoker_agent"))
    assert os.path.isdir(os.path.join(root, "engines"))


def test_build_wires_settings_defaults(inprocess_settings):
    driver = build_inprocess_driver({}, settings=inprocess_settings)
    overrides = driver._env_overrides
    # Workers must talk to loopback, not hairpin through PUBLIC_BASE_URL.
    assert overrides["STOKER_CONTROL_URL"] == (
        "http://127.0.0.1:%d" % inprocess_settings.port)
    # No per-worker prometheus listener (N workers share one netns).
    assert overrides["STOKER_METRICS_PORT"] == "0"
    # The worker layout is on the child's PYTHONPATH (agent + vendored engine).
    root = _find_worker_root()
    assert root in overrides["PYTHONPATH"].split(os.pathsep)
    assert os.path.join(root, "engines", "eventgen") in overrides["PYTHONPATH"]
    assert driver._max_workers == inprocess_settings.inprocess_max_workers


def test_build_fleet_config_overrides_win(inprocess_settings):
    driver = build_inprocess_driver(
        {"control_url": "http://127.0.0.1:9999", "max_workers": 5},
        settings=inprocess_settings)
    assert driver._env_overrides["STOKER_CONTROL_URL"] == "http://127.0.0.1:9999"
    assert driver._max_workers == 5


# --------------------------------------------------------------------------- #
# Worker cap (driver-level backstop; the submit gate is the friendly 422).
# --------------------------------------------------------------------------- #

def _driver(max_workers=2):
    # type: (int) -> InProcessDriver
    return InProcessDriver(
        control_url="http://127.0.0.1:8080",
        worker_root=_find_worker_root() or "/nonexistent",
        max_workers=max_workers)


def test_create_over_cap_raises_before_any_spawn():
    driver = _driver(max_workers=1)
    with mock.patch.object(driver, "_spawn_one") as spawn:
        with pytest.raises(DriverError) as excinfo:
            driver.create(_snapshot(), 2)
    assert "capped at 1" in str(excinfo.value)
    spawn.assert_not_called()


def test_scale_over_cap_raises():
    driver = _driver(max_workers=2)
    with mock.patch.object(driver, "_spawn_one"):
        ref = driver.create(_snapshot(), 1)
        with pytest.raises(DriverError):
            driver.scale(ref, 3)


# --------------------------------------------------------------------------- #
# Per-slot child env.
# --------------------------------------------------------------------------- #

def test_child_env_per_slot_sockets_and_overrides():
    driver = _driver()
    with mock.patch.object(driver, "_spawn_one"):
        ref = driver.create(_snapshot(run_id=917), 2)
    state = driver._fleets[ref.id]

    env0 = driver._child_env(state, 0)
    env1 = driver._child_env(state, 1)
    # The loopback control URL beats the snapshot's public URL.
    assert env0["STOKER_CONTROL_URL"] == "http://127.0.0.1:8080"
    assert env0["STOKER_METRICS_PORT"] == "0"
    assert env0["STOKER_HINT_SLOT"] == "0"
    assert env1["STOKER_HINT_SLOT"] == "1"
    # One unix socket per worker (shared /tmp): distinct, and run-scoped.
    assert env0["STOKER_OUTPUT_SOCKET"] != env1["STOKER_OUTPUT_SOCKET"]
    assert "917" in env0["STOKER_OUTPUT_SOCKET"]


# --------------------------------------------------------------------------- #
# Log capture (FakeDriver plumbing the in-process fleet relies on).
# --------------------------------------------------------------------------- #

def test_capture_logs_streams_child_output(tmp_path):
    # A child that dies instantly with output: PYTHONPATH is emptied so
    # ``python -m stoker_agent`` cannot import, and cwd is an empty tmp dir so
    # no repo layout can rescue it. Its traceback must land in logs().
    driver = FakeDriver(spawn=True, capture_logs=True,
                        env_overrides={"PYTHONPATH": ""}, cwd=str(tmp_path))
    ref = driver.create(_snapshot(run_id=931), 1)
    try:
        deadline = time.time() + 10
        text = ""
        while time.time() < deadline:
            text = driver.logs(ref, None, 100)
            if "stoker_agent" in text:
                break
            time.sleep(0.05)
        assert "stoker_agent" in text, "captured logs: %r" % text
        assert "[slot 0]" in text
    finally:
        driver.destroy(ref)


def test_capture_disabled_keeps_devnull_behaviour(tmp_path):
    driver = FakeDriver(spawn=True, capture_logs=False,
                        env_overrides={"PYTHONPATH": ""}, cwd=str(tmp_path))
    ref = driver.create(_snapshot(run_id=933), 1)
    try:
        # Give the doomed child a moment to exit; nothing must be captured.
        deadline = time.time() + 5
        state = driver._fleets[ref.id]
        while time.time() < deadline and any(
                p.poll() is None for p in state.procs):
            time.sleep(0.05)
        assert driver.logs(ref, None, 100) == ""
    finally:
        driver.destroy(ref)
