"""add specs.rate_shape (time-of-day shaping for eps runs)

Revision ID: 0005_spec_rate_shape
Revises: 0004_spec_eventgen_impl
Create Date: 2026-09-27

``rate_shape = pack`` makes an eps run follow the pack's hourOfDayRate /
dayOfWeekRate / ... maps with the configured eps as the average; null keeps the
flat rate. Defensive like 0003/0004 (skipped when the column already exists).
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_spec_rate_shape"
down_revision = "0004_spec_eventgen_impl"
branch_labels = None
depends_on = None


def _spec_columns(insp):
    # type: (object) -> set
    try:
        return {c["name"] for c in insp.get_columns("specs")}
    except Exception:  # pragma: no cover
        return set()


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "rate_shape" not in _spec_columns(insp):
        op.add_column("specs", sa.Column("rate_shape", sa.String(16), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "rate_shape" in _spec_columns(insp):
        op.drop_column("specs", "rate_shape")
