"""Create the namespace for durable NABD runtime state."""
from alembic import op

revision = "0001_nabd_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA nabd")


def downgrade() -> None:
    # Checkpointer tables have their own migrations. Never cascade-drop that data.
    op.execute("DROP SCHEMA nabd")
