"""widen cumulative counters to 64-bit and index the hot metric/event paths

Revision ID: 0002_bigint_counters_and_indexes
Revises: 0001_baseline
Create Date: 2026-07-16

Two changes to tables that grow unbounded during a soak:

* ``metric_samples.events_total`` / ``bytes_total`` were 32-bit ``Integer``.
  On Postgres that overflows at ~2.1e9 — ``bytes_total`` (cumulative bytes
  delivered by a worker) crosses it in minutes of load, after which every
  heartbeat INSERT raises ``NumericValueOutOfRange`` and the run aborts. Widen
  to ``BigInteger``. SQLite's INTEGER is already 64-bit dynamic, so this is a
  no-op there.
* Add indexes for the hot lookups so ``metric_samples`` (one row per slot per
  ~5 s) and ``run_events`` (append-only, never pruned) do not full-scan:
  ``metric_samples(run_id, slot, ts)`` for the latest-sample read,
  ``metric_samples(ts)`` for the roll-up/prune scans, and
  ``run_events(run_id, ts)`` for the per-run audit reads.

Written defensively (Postgres-only column widen; ``create_index`` skipped when
the index already exists) so it is safe on BOTH paths: an ``alembic upgrade
head`` against an empty DB (where 0001's ``create_all`` already built the
current-model schema, indexes included) and the real upgrade of a live DB that
was stamped at 0001 before this change.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002_bigint_counters_and_indexes"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


_INDEXES = (
    ("ix_metric_samples_run_slot_ts", "metric_samples", ["run_id", "slot", "ts"]),
    ("ix_metric_samples_ts", "metric_samples", ["ts"]),
    ("ix_run_events_run_ts", "run_events", ["run_id", "ts"]),
)


def _existing_indexes(insp, table):
    # type: (object, str) -> set
    try:
        return {ix["name"] for ix in insp.get_indexes(table)}
    except Exception:  # pragma: no cover - table absent is not our concern here
        return set()


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # Postgres-only: widen the 32-bit counters. SQLite INTEGER is already 64-bit,
    # and its batch ALTER (table rebuild) would be needless churn.
    if bind.dialect.name == "postgresql":
        for column in ("events_total", "bytes_total"):
            op.alter_column(
                "metric_samples", column,
                type_=sa.BigInteger(), existing_type=sa.Integer(),
                existing_nullable=True)

    # Create each index only when absent (a fresh DB already has them from the
    # baseline create_all; a legacy 0001-stamped DB does not).
    for name, table, cols in _INDEXES:
        if name not in _existing_indexes(insp, table):
            op.create_index(name, table, cols)


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    for name, table, _cols in _INDEXES:
        if name in _existing_indexes(insp, table):
            op.drop_index(name, table_name=table)

    if bind.dialect.name == "postgresql":
        for column in ("events_total", "bytes_total"):
            op.alter_column(
                "metric_samples", column,
                type_=sa.Integer(), existing_type=sa.BigInteger(),
                existing_nullable=True)
