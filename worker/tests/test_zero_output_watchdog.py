"""Zero-output watchdog: a released eventgen worker that reads nothing from the
engine socket for STOKER_ZERO_OUTPUT_S restarts the engine in place (a fresh
fork clears the non-deterministic multiprocessing hang that leaves the engine
alive but silent at 0 eps), and fails the slot only after exhausting the
restart budget."""

from __future__ import annotations

from unittest import mock

import stoker_agent.agent as agent_mod
from stoker_agent.agent import Agent, EXIT_NO_OUTPUT
from stoker_agent.config import load_config
from stoker_agent.slice import SpecSlice


def _agent(zero_output_s="1", max_restarts="2"):
    return Agent(load_config({
        "STOKER_RUN_ID": "1", "STOKER_CONTROL_URL": "http://ctl.invalid",
        "STOKER_RUN_JWT": "jwt", "STOKER_TOTAL_WORKERS": "3",
        "STOKER_HEC_TOKEN": "tok", "STOKER_METRICS_PORT": "0",
        "STOKER_ZERO_OUTPUT_S": zero_output_s,
        "STOKER_ZERO_OUTPUT_MAX_RESTARTS": max_restarts,
    }))


def _eventgen_slice():
    return SpecSlice.from_claim({
        "run_id": 1, "slot": 0, "total_workers": 3, "lease_id": "le",
        "engine": "eventgen",
        "bundle": {"url": "/tmp/pack"}, "share": {"eps": 100},
        "hec": {"url": "http://h:8088", "index": "loadtest"},
        "telemetry": {"interval_s": 0.01}, "released": True,
    })


def _wire(agent, engine, control):
    agent._engine = engine
    agent._engine_started = True
    agent._sock = mock.Mock(received=0)          # never produces
    agent._hec = mock.Mock()
    agent._hec.snapshot.return_value = {}
    agent._bucket = None                         # skips fencing
    agent._state = "generating"


def _fast_monotonic(monkeypatch):
    # Jump well past the 1 s zero-output window on every call.
    counter = {"t": 0.0}

    def _mono():
        counter["t"] += 100.0
        return counter["t"]

    monkeypatch.setattr(agent_mod.time, "monotonic", _mono)


def _run(agent, control):
    cpu = mock.Mock()
    cpu.sample.return_value = 0.0
    agent._run_loop(control, _eventgen_slice(), None, cpu, mock.Mock(),
                    "/tmp/x.conf", mock.Mock())


def test_stall_restarts_the_engine(monkeypatch):
    agent = _agent()
    engine = mock.Mock()
    engine.is_alive.return_value = True
    # One restart is enough to end the test: stop the loop after it fires.
    engine.restart.side_effect = lambda: agent._drain_event.set()
    control = mock.Mock()
    control.deadman_expired.return_value = False
    control.heartbeat.return_value = None
    _wire(agent, engine, control)
    _fast_monotonic(monkeypatch)

    _run(agent, control)

    engine.restart.assert_called_once()


def test_gives_up_after_the_restart_budget(monkeypatch):
    agent = _agent(max_restarts="2")
    engine = mock.Mock()
    engine.is_alive.return_value = True
    engine.restart.side_effect = lambda: None    # restarts never help
    control = mock.Mock()
    control.deadman_expired.return_value = False
    control.heartbeat.return_value = None
    _wire(agent, engine, control)
    _fast_monotonic(monkeypatch)

    _run(agent, control)

    # Restarted the budgeted number of times, then failed the slot.
    assert engine.restart.call_count == 2
    assert agent._exit_code == EXIT_NO_OUTPUT
    assert agent._drain_event.is_set()


def test_progress_never_triggers_the_watchdog(monkeypatch):
    """A worker that IS reading from the socket is never restarted."""
    agent = _agent()
    engine = mock.Mock()
    engine.is_alive.return_value = True
    control = mock.Mock()
    control.deadman_expired.return_value = False
    control.heartbeat.return_value = None
    _wire(agent, engine, control)
    # received climbs every read; end the loop after a few iterations.
    ticks = {"n": 0}

    class _Sock:
        @property
        def received(self):
            ticks["n"] += 1
            if ticks["n"] > 20:
                agent._drain_event.set()
            return ticks["n"]

    agent._sock = _Sock()
    _fast_monotonic(monkeypatch)

    _run(agent, control)

    engine.restart.assert_not_called()
