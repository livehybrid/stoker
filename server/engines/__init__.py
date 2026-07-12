"""Engine-specific policy: apportionment and per-engine ceilings.

These are pure functions with no DB or driver dependency. The lifecycle layer
calls :func:`server.engines.apportion.apportion_shares` to split a run's rate
across worker slots, and :func:`server.engines.ceilings.check_slice` to reject
a per-worker share that exceeds the conservative ceiling table.
"""

from .apportion import apportion_shares, largest_remainder
from .ceilings import CeilingCheck, check_slice
from .known import DEFAULT_ENGINE, ENGINES, is_known_engine, is_rawreplay

__all__ = [
    "largest_remainder",
    "apportion_shares",
    "check_slice",
    "CeilingCheck",
    "DEFAULT_ENGINE",
    "ENGINES",
    "is_known_engine",
    "is_rawreplay",
]
