"""add pseudonym_keys (the instance key for consistent pseudonymisation)

Revision ID: 0006_pseudonym_keys
Revises: 0005_spec_rate_shape
Create Date: 2026-10-02

One row holds the Fernet-encrypted 32-byte key that every pseudonym stand-in is
derived from, so two packs built on this instance correlate with each other.

**The downgrade does not drop the table, on purpose.** The key is the only copy
of the mapping between an operator's real identifiers and the stand-ins already
written into their packs; dropping it on a rollback would destroy correlation
between packs built before and after, permanently, in exchange for tidiness.
A spare table costs nothing. (Note also that an older control-plane image does
not boot against a database stamped at a newer revision, because init_db runs
`upgrade head` unguarded, so a rollback means downgrading with the NEW image
first and then deploying the old one.)

Defensive like 0003 to 0005: skipped when the table already exists, so a
database adopted by create_all is left untouched.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_pseudonym_keys"
down_revision = "0005_spec_rate_shape"
branch_labels = None
depends_on = None

TABLE = "pseudonym_keys"


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
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("key_encrypted", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(16), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False,
                  server_default="hmac-sha256-v1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.UniqueConstraint("name", name="uq_pseudonym_keys_name"),
    )


def downgrade():
    # Deliberately a no-op: see the module docstring. Dropping this table
    # destroys the only copy of the pseudonym mapping.
    pass
