"""baseline - stamp existing schema

Revision ID: 0001baseline
Revises:
Create Date: 2026-10-02

This migration assumes all pre-existing tables are already present in the database.
It exists solely to give Alembic a known starting point so that subsequent migrations
can be applied additively. Running upgrade() on a fresh database is intentional and
safe only when combined with migration 0002 which creates the actual new tables.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "0001baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This migration is a no-op for existing databases.
    # The existing schema (companies, connections, jobs, work_items, findings,
    # change_log, etc.) was created by app/database.py's _run_migrations()
    # and is already present. We simply declare we are at this revision.
    pass


def downgrade() -> None:
    # Nothing to undo — we created nothing here.
    pass
