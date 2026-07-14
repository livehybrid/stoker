# -*- coding: utf-8 -*-
"""Stoker's metrics worker engine.

Runnable as ``python -m stoker_metrics``. Generates synthetic Splunk metric data
points (``event: "metric"`` with a ``fields`` object) over time following a
configured shape (sine, business double-hump, ...), emitting one multi-metric
envelope per dimension-combination per resolution tick to the agent's unix
socket. See :mod:`stoker_metrics.engine`, :mod:`stoker_metrics.patterns` and
``docs/WORKER-CONTRACT.md``.
"""

from __future__ import absolute_import

from .engine import (
    Config,
    MetricsEngine,
    MetricsError,
    build_series,
    load_config,
    main,
)
from .patterns import PATTERN_TYPES, VALUE_KINDS, activity, sample_value

__all__ = [
    "Config",
    "MetricsEngine",
    "MetricsError",
    "build_series",
    "load_config",
    "main",
    "PATTERN_TYPES",
    "VALUE_KINDS",
    "activity",
    "sample_value",
]
