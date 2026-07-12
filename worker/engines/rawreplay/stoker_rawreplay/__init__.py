# -*- coding: utf-8 -*-
"""PISTON: Stoker's raw-replay worker engine.

Runnable as ``python -m stoker_rawreplay``. Replays a recorded dataset
byte-for-byte to the agent's unix socket in RATE or CADENCE mode, speaking the
same NDJSON envelope protocol as the eventgen ``stoker`` output plugin. See
:mod:`stoker_rawreplay.engine` and ``docs/WORKER-CONTRACT.md``.
"""

from __future__ import absolute_import

from .engine import (
    MODE_CADENCE,
    MODE_RATE,
    Config,
    RawReplayEngine,
    RawReplayError,
    load_config,
    main,
)

__all__ = [
    "MODE_RATE",
    "MODE_CADENCE",
    "Config",
    "RawReplayEngine",
    "RawReplayError",
    "load_config",
    "main",
]
