"""SQLite engine tuning: the PRAGMAs a concurrent control plane depends on.

A file-based control-plane DB must run in WAL with a busy timeout or the
per-heartbeat write serialises under fleet load (see server/db._tune_sqlite).
These assert the engine actually applies them, and that an in-memory DB is not
forced into WAL (unsupported / pointless there).
"""
from __future__ import annotations

import os
import tempfile

from sqlalchemy import text

from server.db import _make_engine


def _pragma(engine, name):
    with engine.connect() as conn:
        return conn.execute(text("PRAGMA %s" % name)).scalar()


def test_file_sqlite_runs_in_wal_with_busy_timeout():
    tmp = tempfile.mkdtemp()
    engine = _make_engine("sqlite:///" + os.path.join(tmp, "t.db"))
    try:
        assert str(_pragma(engine, "journal_mode")).lower() == "wal"
        assert int(_pragma(engine, "synchronous")) == 1  # NORMAL
        assert int(_pragma(engine, "busy_timeout")) == 5000
    finally:
        engine.dispose()


def test_memory_sqlite_not_forced_into_wal():
    engine = _make_engine("sqlite://")
    try:
        # WAL is not meaningful for an in-memory DB: leave it, but the safety
        # PRAGMAs (busy_timeout) still apply harmlessly.
        assert str(_pragma(engine, "journal_mode")).lower() != "wal"
        assert int(_pragma(engine, "busy_timeout")) == 5000
    finally:
        engine.dispose()
