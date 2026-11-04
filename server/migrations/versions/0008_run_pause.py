"""add operator pause bookkeeping to runs

Revision ID: 0008_run_pause
Revises: 0007_sample_engine_cpu
Create Date: 2026-11-04

A run can now be paused and resumed. paused_at records when the current pause
began (null while not paused); paused_s accumulates the seconds spent in
earlier pauses, so a bounded run's end moves out by the time it was paused.

Defensive like 0003 to 0007: a column that already exists (a database adopted
by create_all) is skipped.
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_run_pause"
down_revision = "0007_sample_engine_cpu"
branch_labels = None
depends_on = None

TABLE = "runs"


def _existing(insp):
    # type: (object) -> set
    try:
        return {c["name"] for c in insp.get_columns(TABLE)}
    except Exception:  # pragma: no cover - defensive
        return set()


def upgrade():
    have = _existing(sa.inspect(op.get_bind()))
    if "paused_at" not in have:
        op.add_column(TABLE, sa.Column("paused_at", sa.DateTime(timezone=True),
                                       nullable=True))
    if "paused_s" not in have:
        op.add_column(TABLE, sa.Column("paused_s", sa.Float(), nullable=False,
                                       server_default="0"))


def downgrade():
    have = _existing(sa.inspect(op.get_bind()))
    for name in ("paused_s", "paused_at"):
        if name in have:
            op.drop_column(TABLE, name)
