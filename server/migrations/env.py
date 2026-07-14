"""Alembic environment for Stoker.

Adoption model (see ``server/migrate.py``): ``init_db()`` drives migrations on
boot. A fresh or legacy ``create_all`` schema is ``create_all``'d then stamped at
head; a managed DB is upgraded. This env also backs the CLI for authoring delta
revisions: ``alembic revision --autogenerate -m "..."``.

``run_migrations()`` injects a live connection via ``config.attributes`` so the
migration runs on the app's engine; the CLI path builds an engine from the URL,
resolved from Stoker settings when ``sqlalchemy.url`` is unset in alembic.ini.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from server import models  # noqa: F401  (register every model on Base.metadata)
from server.db import Base

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # pragma: no cover - logging config is best-effort
        pass

target_metadata = Base.metadata


def _url():
    # type: () -> str
    """The DB URL: an explicit alembic.ini value, else the app settings."""
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from server.config import get_settings

    return get_settings().database_url


def run_migrations_offline():
    # type: () -> None
    """Emit SQL to stdout (``alembic upgrade --sql``); no DB connection."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    # type: () -> None
    """Run against a live connection (injected by run_migrations, else built)."""
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        context.configure(
            connection=connectable, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
