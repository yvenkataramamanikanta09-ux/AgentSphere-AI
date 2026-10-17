"""Authentication and workspace isolation; refuse unassigned legacy documents."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade():
    # Recheck under a lock: an earlier count alone cannot prevent concurrent inserts.
    op.execute("LOCK TABLE documents IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM documents) THEN
            RAISE EXCEPTION 'Stopped: explicit ownership mapping required. No data changed.';
        END IF;
    END $$""")
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        *timestamps(),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_workspaces_owner_user_id", "workspaces", ["owner_user_id"])
    op.create_table(
        "workspace_memberships",
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("role", sa.String(10), nullable=False),
        *timestamps(),
        sa.CheckConstraint("role IN ('editor', 'viewer')", name="ck_membership_role"),
    )
    op.create_index("ix_workspace_memberships_user_id", "workspace_memberships", ["user_id"])
    op.create_table(
        "auth_rate_limits",
        sa.Column("scope", sa.String(32), primary_key=True),
        sa.Column("identity_digest", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.BigInteger(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_rate_limits_expires_at", "auth_rate_limits", ["expires_at"])
    op.add_column("documents", sa.Column("workspace_id", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        "fk_documents_workspace", "documents", "workspaces", ["workspace_id"], ["id"]
    )
    op.drop_index("ix_documents_created_at_id", table_name="documents")
    op.create_index(
        "ix_documents_workspace_created_id", "documents", ["workspace_id", "created_at", "id"]
    )


def downgrade():
    # Never silently destroy accounts or remove isolation from real documents.
    op.execute(
        "LOCK TABLE documents, users, workspaces, sessions, auth_rate_limits "
        "IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM users) OR EXISTS (SELECT 1 FROM documents)
           OR EXISTS (SELECT 1 FROM sessions) OR EXISTS (SELECT 1 FROM auth_rate_limits) THEN
            RAISE EXCEPTION 'Downgrade refused: authentication or document data exists.';
        END IF;
    END $$""")
    op.drop_index("ix_documents_workspace_created_id", table_name="documents")
    op.drop_constraint("fk_documents_workspace", "documents", type_="foreignkey")
    op.drop_column("documents", "workspace_id")
    op.create_index("ix_documents_created_at_id", "documents", ["created_at", "id"])
    for table in ("auth_rate_limits", "workspace_memberships", "sessions", "workspaces", "users"):
        op.drop_table(table)
