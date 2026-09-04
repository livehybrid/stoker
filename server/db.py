"""SQLAlchemy 2.0 engine, session factory and FastAPI dependency.

The DB is the source of truth for the whole control plane. Prod is Postgres
(``postgresql+psycopg://``); local dev and the test suite run on SQLite. Models
are dialect-agnostic (see :mod:`server.models`), so the same schema
``create_all``s on either backend.

``engine`` / ``SessionLocal`` are created lazily from :func:`get_settings` so
tests can swap ``DATABASE_URL`` before the first connection. Call
:func:`configure` to (re)bind them to an explicit URL.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings

log = logging.getLogger("stoker.db")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine = None  # type: Optional[Engine]
_SessionLocal = None  # type: Optional[sessionmaker[Session]]


def _make_engine(database_url):
    # type: (str) -> Engine
    """Build an engine tuned for the URL's dialect.

    SQLite needs ``check_same_thread=False`` (the supervisor loop and request
    handlers share it). A pure in-memory SQLite URL additionally needs a
    ``StaticPool`` so every connection sees the same database.
    """
    connect_args = {}
    kwargs = {"future": True, "pool_pre_ping": True}
    is_memory = False
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        # ":memory:" (or the sqlite:// shorthand) must share one connection.
        is_memory = ":memory:" in database_url or database_url in ("sqlite://",)
        if is_memory:
            kwargs["poolclass"] = StaticPool
            kwargs.pop("pool_pre_ping", None)
    kwargs["connect_args"] = connect_args
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        _tune_sqlite(engine, is_memory)
    return engine


def _tune_sqlite(engine, is_memory):
    # type: (Engine, bool) -> None
    """Set the SQLite PRAGMAs a concurrent control plane needs.

    The default (rollback journal, ``synchronous=FULL``, no ``busy_timeout``)
    takes a whole-file EXCLUSIVE lock per write and blocks readers, so at fleet
    scale the per-heartbeat write (a ``metric_samples`` insert + lease update +
    commit) serialises and each can stall for seconds — a benchmark of 30
    concurrent writers fell to ~35 commits/s at a p99 near 5 s with lock errors.
    That backs up the request threadpool, delays release/heartbeat responses and
    lets a warmed-but-paused engine wedge at 0 eps.

    * ``journal_mode=WAL`` — readers no longer block the single writer; a write
      no longer locks the whole file (file-based DBs only; a no-op for
      ``:memory:``). Persists on the file, but re-asserting per connection is
      harmless.
    * ``synchronous=NORMAL`` — safe under WAL (a crash loses at most the last
      transaction, never corrupts) and removes a per-commit fsync.
    * ``busy_timeout=5000`` — wait up to 5 s for a lock instead of raising
      ``SQLITE_BUSY`` immediately.

    The same 30-writer benchmark rises to ~1600 commits/s at a p99 of ~330 ms
    with zero lock errors. Per-connection settings, so applied on every connect.
    """
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        try:
            if not is_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def configure(database_url=None):
    # type: (Optional[str]) -> Engine
    """(Re)create the engine and session factory bound to ``database_url``.

    When ``database_url`` is None the value comes from :func:`get_settings`.
    Safe to call repeatedly; the previous engine is disposed first.
    """
    global _engine, _SessionLocal
    if database_url is None:
        database_url = get_settings().database_url
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(database_url)
    _SessionLocal = sessionmaker(
        bind=_engine, autoflush=False, autocommit=False,
        expire_on_commit=False, class_=Session, future=True,
    )
    log.debug("configured database engine for %s", _engine.url.render_as_string(hide_password=True))
    return _engine


def get_engine():
    # type: () -> Engine
    """Return the active engine, creating it from settings on first use."""
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def get_session_factory():
    # type: () -> sessionmaker[Session]
    """Return the active session factory, creating it on first use."""
    if _SessionLocal is None:
        configure()
    assert _SessionLocal is not None
    return _SessionLocal


def SessionLocal():
    # type: () -> Session
    """Create a new ORM session (context-manager friendly)."""
    return get_session_factory()()


def get_db():
    # type: () -> Iterator[Session]
    """FastAPI dependency yielding a request-scoped session.

    Commits on clean exit, rolls back on exception, always closes.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all():
    # type: () -> None
    """Create every table for the registered models (idempotent)."""
    # Import for the side effect of registering models on ``Base.metadata``.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())
    log.debug("create_all complete")


def init_db():
    # type: () -> None
    """Ensure the engine exists and the schema is at head.

    Schema is managed by Alembic (:mod:`server.migrate`): an unmanaged DB (fresh
    or a legacy ``create_all`` schema, including the live Postgres) is
    ``create_all``'d and stamped at head; a managed DB is upgraded. ``create_all``
    remains the direct path for the test suite's isolated SQLite DBs.
    """
    configure()
    from .migrate import run_migrations

    run_migrations()
