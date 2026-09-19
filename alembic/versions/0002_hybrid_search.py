"""Add the sparse leg of hybrid retrieval: a generated tsvector on chunks + GIN index.

Vector search alone misses exact tokens (a part number, a model code, a person's
name) because the embedding blurs them into semantics. Postgres full-text search
matches them literally. The column is GENERATED so it cannot drift from `content`.

Revision ID: 0002_hybrid
Revises: 0001_detach
"""

from alembic import op

revision = "0002_hybrid"
down_revision = "0001_detach"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_tsv_gin ON chunks USING gin (tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv_gin")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS tsv")
