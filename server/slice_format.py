"""ISO 8601 timestamp formatting/parsing that matches the worker byte-for-byte.

The worker's ``stoker_agent/slice.py`` formats release/effective-t0 timestamps
as ``<iso>Z`` (UTC, trailing ``Z``) and parses the same. The control plane must
emit exactly that shape in ``release`` commands and slice ``effective_t0`` so the
worker's ``parse_iso8601`` round-trips it. This module is a standalone copy of
those two functions (the worker package is a different import root, not a server
dependency), kept identical on purpose.
"""

from __future__ import annotations

import datetime


def format_iso8601(epoch):
    # type: (float) -> str
    """Format epoch seconds as an ISO 8601 UTC string ending in ``Z``."""
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def parse_iso8601(value):
    # type: (str) -> float
    """Parse an ISO 8601 timestamp (Z or numeric offset) to epoch seconds."""
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def now_iso8601():
    # type: () -> str
    """Current UTC instant as an ISO 8601 ``Z`` string."""
    return format_iso8601(datetime.datetime.now(datetime.timezone.utc).timestamp())


__all__ = ["format_iso8601", "parse_iso8601", "now_iso8601"]
