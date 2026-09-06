"""Detach business records from transcripts before retention purge exists.

Leads and appointments cascaded from conversations, so a data-retention purge would
have deleted the client's leads and bookings along with the chat transcript. They are
different records with different retention bases - the link becomes SET NULL.

Revision ID: 0001_detach
"""

from alembic import op

revision = "0001_detach"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("leads", "appointments"):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN conversation_id DROP NOT NULL")
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_conversation_id_fkey"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_conversation_id_fkey "
            f"FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL"
        )


def downgrade() -> None:
    for table in ("leads", "appointments"):
        op.execute(f"DELETE FROM {table} WHERE conversation_id IS NULL")
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_conversation_id_fkey"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_conversation_id_fkey "
            f"FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN conversation_id SET NOT NULL")


# --- consent columns (same revision; the dev DB is altered directly below) ---
def upgrade_consent() -> None:
    op.execute(
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS consent_at timestamptz"
    )
    op.execute(
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS consent_version integer "
        "NOT NULL DEFAULT 0"
    )
