"""Boot-time schema migration via Alembic.

Adoption-safe for a codebase that shipped on ``create_all``: an UNMANAGED
database (no ``alembic_version`` table — a fresh install OR the existing
``create_all`` schema, including the live Postgres) is ``create_all``'d to the
current models and STAMPED at head. No per-table DDL is replayed, so an existing
database with data is adopted untouched. A MANAGED database is upgraded to head,
applying any delta revisions written since.

This relies on the same invariant Alembic needs anyway: ``create_all`` (the
models) equals head (baseline + deltas). ``test_migrations`` guards it, and
``alembic revision --autogenerate`` producing an empty diff is the CI check.
"""
from __future__ import annotations

import logging
import os

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from .db import create_all, get_engine

log = logging.getLogger("stoker.migrate")

_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def _alembic_config(connection):
    # type: (object) -> Config
    """An Alembic Config bound to a live connection (env.py runs on it)."""
    cfg = Config()
    cfg.set_main_option("script_location", _MIGRATIONS_DIR)
    cfg.attributes["connection"] = connection
    return cfg


def _current_revision(connection):
    # type: (object) -> object
    """The DB's Alembic revision, or None when the DB is unmanaged.

    ``get_current_revision`` returns None when the ``alembic_version`` table is
    absent, which is exactly how we detect a fresh / legacy create_all DB.
    """
    return MigrationContext.configure(connection).get_current_revision()


def run_migrations():
    # type: () -> None
    """Bring the active engine's schema to head (see the module docstring)."""
    engine = get_engine()

    # 1. Decide managed vs unmanaged on a read-only connection, then release it
    #    (so we never hold a write lock while create_all runs on SQLite).
    with engine.connect() as connection:
        managed = _current_revision(connection) is not None

    # 2. Unmanaged (fresh or legacy create_all): ensure the current schema exists
    #    first. Idempotent, so a live DB with data is left untouched.
    if not managed:
        create_all()

    # 3. Run the Alembic command inside a committing transaction (engine.begin);
    #    env.py runs on this connection, and begin() commits the alembic_version
    #    write on clean exit.
    with engine.begin() as connection:
        cfg = _alembic_config(connection)
        if managed:
            log.info("alembic: upgrading schema to head")
            command.upgrade(cfg, "head")
        else:
            command.stamp(cfg, "head")
            log.info("alembic: adopted existing/fresh schema and stamped at head")
