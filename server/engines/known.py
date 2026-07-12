"""The set of worker engines the control plane knows about.

One source of truth for engine names so the spec schema validation, the pack
linter/bundler and the git-sync indexer all agree. Keep in step with the
worker's ``stoker_agent.config.ENGINES`` (the control plane may know an engine
the worker also accepts via ``STOKER_ENGINE``).

* ``eventgen`` — templates events from samples (the original engine).
* ``rawreplay`` — Piston: replays a recorded dataset byte-for-byte, re-stamped
  to now, at a chosen rate (RATE mode, agent-paced) or the recorded cadence
  (CADENCE mode, engine-paced). A rawreplay pack declares no ``eventgen.conf``;
  it declares a ``replay:`` section (dataset + mode + time_multiple).
"""

from __future__ import annotations

# Ordered, most-common first. ``DEFAULT_ENGINE`` is what a spec/pack assumes
# when nothing declares one.
DEFAULT_ENGINE = "eventgen"
ENGINES = ("eventgen", "rawreplay")


def is_known_engine(name):
    # type: (str) -> bool
    """True when ``name`` is an engine the control plane can register/run."""
    return name in ENGINES


def is_rawreplay(name):
    # type: (object) -> bool
    """True when ``name`` selects the rawreplay (Piston) engine."""
    return name == "rawreplay"


__all__ = ["DEFAULT_ENGINE", "ENGINES", "is_known_engine", "is_rawreplay"]
