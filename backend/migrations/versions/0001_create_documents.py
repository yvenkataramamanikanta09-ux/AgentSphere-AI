"""Create document metadata storage. Downgrade removes the table and its data."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "registered",
                "archived",
                native_enum=False,
                create_constraint=True,
                name="document_status",
            ),
            nullable=False,
            server_default="registered",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_documents_positive_size"),
        sa.CheckConstraint("length(trim(filename)) > 0", name="ck_documents_filename"),
        sa.CheckConstraint("length(trim(content_type)) > 0", name="ck_documents_content_type"),
    )
    op.create_index("ix_documents_created_at_id", "documents", ["created_at", "id"])


def downgrade() -> None:
    op.drop_table("documents")
