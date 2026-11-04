"""Operator pause / resume commands on the agent."""

from __future__ import annotations

import types

from stoker_agent.agent import Agent
from stoker_agent.pacing import TokenBucket


class _Clock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Engine(object):
    def __init__(self):
        self.restarts = 0

    def restart(self):
        self.restarts += 1

    def is_alive(self):
        return True


def _agent(engine="eventgen", backfill_start_s=None):
    clock = _Clock()
    agent = Agent(types.SimpleNamespace(standalone=False), clock=clock)
    agent._bucket = TokenBucket(100.0, 5.0, clock)
    agent._bucket.anchor_at(clock())
    agent._engine = _Engine()
    agent._engine_started = True
    agent._state = "generating"
    sl = types.SimpleNamespace(engine=engine, backfill_start_s=backfill_start_s,
                               rate_mode="eps")
    return agent, sl, clock


def test_pause_holds_output_and_resume_releases_without_a_burst():
    agent, sl, clock = _agent()
    agent._apply_command({"command": "pause", "paused_s": 0.0}, sl, "", None)
    assert agent._bucket.paused
    assert agent._state == "paused"
    assert agent._bucket.try_take() is False

    clock.t += 120.0  # two minutes paused
    agent._apply_command({"command": "continue", "paused_s": 120.0}, sl, "", None)
    assert not agent._bucket.paused
    assert agent._state == "generating"
    assert agent._paused_s == 120.0
    # Re-anchored at resume: nothing from the paused interval is owed.
    assert agent._bucket.lag_s() == 0.0


def test_repeated_pause_is_idempotent():
    agent, sl, _ = _agent()
    agent._apply_command({"command": "pause"}, sl, "", None)
    agent._apply_command({"command": "pause"}, sl, "", None)
    assert agent._operator_paused and agent._bucket.paused


def test_drain_while_paused_does_not_resume():
    agent, sl, _ = _agent()
    agent._apply_command({"command": "pause"}, sl, "", None)
    agent._apply_command({"command": "drain"}, sl, "", None)
    assert agent._drain_event.is_set()
    assert agent._bucket.paused


def test_fence_lift_does_not_undo_an_operator_pause():
    agent, sl, _ = _agent()
    agent._apply_command({"command": "pause"}, sl, "", None)
    agent._fencing_paused = True
    control = types.SimpleNamespace(should_pause=lambda: False)
    agent._check_fencing(control)
    assert agent._bucket.paused
    assert agent._state == "paused"


def test_live_metrics_engine_restarts_on_resume_backfill_does_not():
    agent, sl, _ = _agent(engine="metrics")
    agent._apply_command({"command": "pause"}, sl, "", None)
    agent._apply_command({"command": "continue"}, sl, "", None)
    assert agent._engine.restarts == 1

    agent, sl, _ = _agent(engine="metrics", backfill_start_s=0.0)
    agent._apply_command({"command": "pause"}, sl, "", None)
    agent._apply_command({"command": "continue"}, sl, "", None)
    assert agent._engine.restarts == 0


def test_bad_paused_s_is_ignored():
    agent, sl, _ = _agent()
    agent._apply_command({"command": "continue", "paused_s": "lots"}, sl, "", None)
    agent._apply_command({"command": "continue", "paused_s": -5}, sl, "", None)
    assert agent._paused_s == 0.0
