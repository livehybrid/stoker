"""add engine CPU, generator threads and CPU limit to metric_samples

Revision ID: 0007_sample_engine_cpu
Revises: 0006_pseudonym_keys
Create Date: 2026-10-28

cpu_pct has only ever measured the worker agent; the engine subprocess that
does the generating went unmeasured, so a worker pinned at its core allowance
read as idle. Three nullable columns: engine_cpu_pct (100 = one core),
engine_threads, and cpu_limit_cores (the container's quota). Rows from older
workers leave them null.

Defensive like 0003 to 0006: a column that already exists (a database adopted
by create_all) is skipped.
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_sample_engine_cpu"
down_revision = "0006_pseudonym_keys"
branch_labels = None
depends_on = None

TABLE = "metric_samples"
COLUMNS = (
    ("engine_cpu_pct", sa.Float()),
    ("engine_threads", sa.Integer()),
    ("cpu_limit_cores", sa.Float()),
)


def _existing(insp):
    # type: (object) -> set
    try:
        return {c["name"] for c in insp.get_columns(TABLE)}
    except Exception:  # pragma: no cover - defensive
        return set()


def upgrade():
    have = _existing(sa.inspect(op.get_bind()))
    for name, kind in COLUMNS:
        if name not in have:
            op.add_column(TABLE, sa.Column(name, kind, nullable=True))


def downgrade():
    have = _existing(sa.inspect(op.get_bind()))
    for name, _kind in COLUMNS:
        if name in have:
            op.drop_column(TABLE, name)
