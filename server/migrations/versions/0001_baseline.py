"""baseline: the create_all schema at Alembic adoption (2026-07-12)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-12

The baseline reproduces the schema that ``server.db.create_all()`` builds from
the models, using the models' own metadata so column types stay dialect-correct
(JSONB on Postgres, JSON on SQLite via the ``with_variant`` declaration in
``server.models``).

On the real boot path (``server.migrate.run_migrations``) a fresh or legacy DB
is ``create_all``'d and stamped at head, so this ``upgrade()`` runs only for a
pure ``alembic upgrade head`` against a genuinely empty DB. Every schema change
after this ships as its own delta revision chaining ``down_revision`` to here.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Build every table from the live model metadata (dialect-correct types).
    from server import models  # noqa: F401  (register models on Base.metadata)
    from server.db import Base

    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    from server import models  # noqa: F401
    from server.db import Base

    Base.metadata.drop_all(bind=op.get_bind())
