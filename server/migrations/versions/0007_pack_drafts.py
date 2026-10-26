"""add pack_drafts (pack builder sessions saved before they build)

Revision ID: 0007_pack_drafts
Revises: 0006_pseudonym_keys
Create Date: 2026-10-26

A draft holds the operator's original, un-pseudonymised sample, so its state
is stored as Fernet ciphertext only. Dropping the table on a downgrade loses
nothing a built pack depends on.

Defensive like 0003 to 0006: skipped when the table already exists, so a
database adopted by create_all is left untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_pack_drafts"
down_revision = "0006_pseudonym_keys"
branch_labels = None
depends_on = None

TABLE = "pack_drafts"


def _has_table(insp):
    # type: (object) -> bool
    try:
        return TABLE in insp.get_table_names()
    except Exception:  # pragma: no cover - defensive
        return False


def upgrade():
    insp = sa.inspect(op.get_bind())
    if _has_table(insp):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, server_default=""),
        sa.Column("pack_id", sa.Integer(),
                  sa.ForeignKey("packs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("state_encrypted", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if _has_table(insp):
        op.drop_table(TABLE)
