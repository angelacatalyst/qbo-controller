"""milestone_1a_security_foundation

Revision ID: 0002m1asecurity
Revises: 0001baseline
Create Date: 2026-10-02

Creates the four security foundation tables:
  - users
  - user_sessions
  - company_accesses
  - oauth_states

upgrade()   — creates these 4 tables (additive, no existing data touched)
downgrade() — drops these 4 tables only (non-destructive to existing tables)
"""

from alembic import op
import sqlalchemy as sa

revision = "0002m1asecurity"
down_revision = "0001baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("role", sa.String(30), nullable=False, server_default="controller"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # ------------------------------------------------------------- user_sessions
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True)

    # --------------------------------------------------------- company_accesses
    op.create_table(
        "company_accesses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("company_id", sa.String(36), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("realm_id", sa.String(100), nullable=False),
        sa.Column("role", sa.String(30), nullable=False, server_default="controller"),
        sa.Column("granted_by", sa.String(36), nullable=True),
        sa.Column("granted_at", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_company_accesses_user_id", "company_accesses", ["user_id"])
    op.create_index("ix_company_accesses_realm_id", "company_accesses", ["realm_id"])

    # ------------------------------------------------------------ oauth_states
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_oauth_states_state", "oauth_states", ["state"], unique=True)


def downgrade() -> None:
    # Drop in reverse-dependency order — additive only, no pre-existing table touched
    op.drop_table("oauth_states")
    op.drop_table("company_accesses")
    op.drop_table("user_sessions")
    op.drop_table("users")
