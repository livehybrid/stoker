"""Alembic boot-migration adoption tests (``server.migrate.run_migrations``).

The control plane shipped on ``create_all``. These prove the migration wiring:

* a fresh DB is built and left Alembic-managed at head;
* a legacy ``create_all`` schema (the live DB's shape) is adopted WITHOUT
  replaying DDL or losing data;
* ``run_migrations`` is idempotent;
* the baseline migration itself builds the schema via a plain ``alembic upgrade``
  (the CLI path).
"""
from __future__ import annotations

import os

from sqlalchemy import inspect, select, text

from server import db as db_mod


def _use(tmp_path, name):
    # type: (object, str) -> str
    url = "sqlite:///%s" % (tmp_path / name)
    db_mod.configure(url)
    return url


def _tables():
    # type: () -> set
    return set(inspect(db_mod.get_engine()).get_table_names())


def _head_rev():
    # type: () -> object
    with db_mod.get_engine().connect() as c:
        return c.execute(text("select version_num from alembic_version")).scalar()


def test_fresh_db_is_migrated_and_managed(tmp_path):
    _use(tmp_path, "fresh.db")
    from server.migrate import run_migrations

    run_migrations()
    assert {"runs", "specs", "targets", "api_tokens", "alembic_version"} <= _tables()
    assert _head_rev()  # non-null: managed at head


def test_legacy_create_all_db_is_adopted_without_data_loss(tmp_path):
    _use(tmp_path, "legacy.db")
    # Simulate the live DB: schema from create_all, no alembic_version, data present.
    db_mod.create_all()
    assert "alembic_version" not in _tables()
    from server.models import Fleet

    with db_mod.SessionLocal() as s:
        s.add(Fleet(name="keep-me", driver="fake", config_json={}))
        s.commit()

    from server.migrate import run_migrations

    run_migrations()

    assert "alembic_version" in _tables()  # now managed
    assert _head_rev()
    with db_mod.SessionLocal() as s:
        names = [f.name for f in s.execute(select(Fleet)).scalars().all()]
    assert "keep-me" in names  # adopted in place, not rebuilt


def test_run_migrations_is_idempotent(tmp_path):
    _use(tmp_path, "idem.db")
    from server.migrate import run_migrations

    run_migrations()
    rev1 = _head_rev()
    run_migrations()  # second run: managed -> upgrade head, no error
    assert _head_rev() == rev1
    assert "runs" in _tables()


def test_head_equals_models_no_pending_autogen(tmp_path):
    """The create_all == head invariant the boot adoption relies on: after
    migrating, ``alembic --autogenerate`` finds no table/column changes vs the
    models. If a model gains a column without the schema tracking it, this fails.
    """
    _use(tmp_path, "autogen.db")
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from server import models  # noqa: F401  (register on Base.metadata)
    from server.db import Base
    from server.migrate import run_migrations

    run_migrations()
    with db_mod.get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    material = [d for d in diff if isinstance(d, tuple) and d and str(d[0]).startswith(
        ("add_table", "remove_table", "add_column", "remove_column"))]
    assert not material, "schema drift from the models: %r" % material


def test_delta_0003_adds_specs_extra_pack_ids(tmp_path):
    """A live DB stamped at 0002 (before multi-pack specs) gains the
    ``specs.extra_pack_ids_json`` column from the 0003 delta — the real upgrade
    path, distinct from a fresh DB where the baseline create_all already has it.
    Simulated by migrating to head, dropping the column and re-stamping at 0002."""
    _use(tmp_path, "delta0003.db")
    from alembic import command
    from alembic.config import Config

    from server.migrate import run_migrations

    run_migrations()

    def _spec_columns():
        # type: () -> set
        return {c["name"] for c in inspect(db_mod.get_engine()).get_columns("specs")}

    assert "extra_pack_ids_json" in _spec_columns()  # fresh DB: baseline has it

    # Rewind to the pre-0003 shape: drop the column, stamp at 0002.
    with db_mod.get_engine().begin() as conn:
        conn.execute(text("ALTER TABLE specs DROP COLUMN extra_pack_ids_json"))
        cfg = Config()
        cfg.set_main_option(
            "script_location",
            os.path.join(os.path.dirname(db_mod.__file__), "migrations"))
        cfg.attributes["connection"] = conn
        command.stamp(cfg, "0002_bigint_counters_and_indexes", purge=True)
    assert "extra_pack_ids_json" not in _spec_columns()

    run_migrations()  # managed at 0002 -> upgrade head applies 0003
    assert _head_rev() == "0003_spec_extra_pack_ids"
    assert "extra_pack_ids_json" in _spec_columns()


def test_baseline_upgrade_builds_schema_via_cli(tmp_path):
    """A plain ``alembic upgrade head`` on an empty DB runs the baseline and
    creates the schema (the manual CLI path, distinct from run_migrations)."""
    url = _use(tmp_path, "cli.db")
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(db_mod.__file__), "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")  # env.py builds its own engine + runs the baseline
    assert {"runs", "api_tokens", "alembic_version"} <= _tables()
