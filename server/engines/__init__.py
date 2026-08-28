"""Engine-specific policy: apportionment, per-engine ceilings and sharding.

These are pure functions with no DB or driver dependency. The lifecycle layer
calls :func:`server.engines.apportion.apportion_shares` to split a run's rate
across worker slots, and :func:`server.engines.ceilings.check_slice` to reject
a per-worker share that exceeds the resolved ceiling (built-in table + env
config + per-fleet override, layered by
:func:`server.engines.ceilings.resolve_ceilings`). The submit route calls
:func:`server.engines.sharding.check_sharding` to reject a fleet larger than
the discrete work its engine can shard (metrics series / count_interval
counts), so no worker sits silently idle for the whole run.
"""

from .apportion import apportion_shares, largest_remainder
from .ceilings import CeilingCheck, check_slice, resolve_ceilings
from .known import DEFAULT_ENGINE, ENGINES, is_known_engine, is_rawreplay
from .sharding import ShardingCheck, check_sharding

__all__ = [
    "largest_remainder",
    "apportion_shares",
    "check_slice",
    "resolve_ceilings",
    "CeilingCheck",
    "check_sharding",
    "ShardingCheck",
    "DEFAULT_ENGINE",
    "ENGINES",
    "is_known_engine",
    "is_rawreplay",
]
