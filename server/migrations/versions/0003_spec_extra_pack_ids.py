"""add specs.extra_pack_ids_json for multi-pack (merged-bundle) specs

Revision ID: 0003_spec_extra_pack_ids
Revises: 0002_bigint_counters_and_indexes
Create Date: 2026-08-28

A spec can now reference additional packs beyond its primary ``pack_id``; the
control plane merges the selected eventgen packs into one synthesised bundle at
provision time (``server.bundles.build_from_packs``), so the worker contract is
unchanged. The extra ids live in a nullable JSON list column rather than a join
table: the list is tiny, ordering is irrelevant (the merge sorts
deterministically by namespace) and nothing queries by it. Null/absent keeps
the classic single-pack behaviour byte-for-byte.

Written defensively (``add_column`` skipped when the column already exists) so
it is safe on BOTH paths: an ``alembic upgrade head`` against an empty DB
(where 0001's ``create_all`` already built the current-model schema, this
column included) and the real upgrade of a live DB stamped at 0002.
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003_spec_extra_pack_ids"
down_revision = "0002_bigint_counters_and_indexes"
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
    if "extra_pack_ids_json" not in _spec_columns(insp):
        # Same dialect-variant JSON type the models declare (JSONB on Postgres,
        # generic JSON on SQLite) so the column matches a create_all schema.
        from server.models import JSON_VARIANT

        op.add_column(
            "specs",
            sa.Column("extra_pack_ids_json", JSON_VARIANT, nullable=True))


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "extra_pack_ids_json" in _spec_columns(insp):
        op.drop_column("specs", "extra_pack_ids_json")
