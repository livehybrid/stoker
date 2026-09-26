"""add specs.eventgen_impl + specs.fast_envelope (engine implementation knob)

Revision ID: 0004_spec_eventgen_impl
Revises: 0003_spec_extra_pack_ids
Create Date: 2026-09-26

A spec can now pin which eventgen implementation its workers run
(``eventgen_impl``: firebox | python, null = auto) and whether firebox uses the
HEC-line socket envelope (``fast_envelope``: null/true = yes, false = classic).
The control plane projects them as ``STOKER_EVENTGEN_IMPL`` /
``STOKER_FAST_ENVELOPE`` only when set, so every existing spec's worker env is
unchanged.

Written defensively (``add_column`` skipped when the column already exists) so
it is safe on BOTH paths: an ``alembic upgrade head`` against an empty DB
(where 0001's ``create_all`` already built the current-model schema, these
columns included) and the real upgrade of a live DB stamped at 0003.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004_spec_eventgen_impl"
down_revision = "0003_spec_extra_pack_ids"
branch_labels = None
depends_on = None


def _spec_columns(insp):
    # type: (object) -> set
    try:
        return {c["name"] for c in insp.get_columns("specs")}
    except Exception:  # pragma: no cover - table absent is not our concern here
        return set()


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = _spec_columns(insp)
    if "eventgen_impl" not in cols:
        op.add_column("specs", sa.Column("eventgen_impl", sa.String(16), nullable=True))
    if "fast_envelope" not in cols:
        op.add_column("specs", sa.Column("fast_envelope", sa.Boolean(), nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = _spec_columns(insp)
    if "fast_envelope" in cols:
        op.drop_column("specs", "fast_envelope")
    if "eventgen_impl" in cols:
        op.drop_column("specs", "eventgen_impl")
