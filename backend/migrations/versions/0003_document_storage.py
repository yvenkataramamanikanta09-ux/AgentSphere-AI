"""Private document files, ordered text sections, and durable processing jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

STORAGE_KEY_CHECK = "storage_key ~ '^objects/[0-9a-f]{32}/[0-9a-f]{32}/original$'"
SAFE_CODE_CHECK = "{column} IS NULL OR {column} ~ '^[a-z][a-z0-9_]{{0,63}}$'"


def upgrade():
    # Additive only: existing metadata, authentication, and workspace rows are untouched.
    op.create_table(
        "document_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("storage_key", sa.String(82), unique=True, nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "uploaded_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "processing_status",
            sa.Enum(
                "queued",
                "processing",
                "ready",
                "failed",
                name="document_processing_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("error_code", sa.String(64)),
        sa.Column("warning_code", sa.String(64)),
        sa.Column("parser_version", sa.String(64)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(STORAGE_KEY_CHECK, name="ck_document_files_storage_key"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_document_files_sha256"),
        sa.CheckConstraint(
            "length(trim(original_filename)) > 0", name="ck_document_files_filename"
        ),
        sa.CheckConstraint(
            SAFE_CODE_CHECK.format(column="error_code"), name="ck_document_files_error_code"
        ),
        sa.CheckConstraint(
            SAFE_CODE_CHECK.format(column="warning_code"), name="ck_document_files_warning_code"
        ),
        sa.CheckConstraint(
            "parser_version IS NULL OR length(trim(parser_version)) > 0",
            name="ck_document_files_parser_version",
        ),
    )
    op.create_index(
        "ix_document_files_processing_uploaded",
        "document_files",
        ["processing_status", "uploaded_at"],
    )
    op.create_index("ix_document_files_uploaded_by", "document_files", ["uploaded_by"])
    op.create_table(
        "document_sections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer()),
        sa.Column("heading", sa.String(500)),
        sa.Column("line_start", sa.Integer()),
        sa.Column("line_end", sa.Integer()),
        sa.CheckConstraint("ordinal >= 0", name="ck_document_sections_ordinal"),
        sa.CheckConstraint("length(text) > 0", name="ck_document_sections_text"),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number >= 1", name="ck_document_sections_page"
        ),
        sa.CheckConstraint(
            "(line_start IS NULL AND line_end IS NULL) OR "
            "(line_start IS NOT NULL AND line_end IS NOT NULL "
            "AND line_start >= 1 AND line_end >= line_start)",
            name="ck_document_sections_lines",
        ),
        sa.CheckConstraint(
            "heading IS NULL OR length(trim(heading)) > 0", name="ck_document_sections_heading"
        ),
    )
    op.create_index(
        "uq_document_sections_document_ordinal",
        "document_sections",
        ["document_id", "ordinal"],
        unique=True,
    )
    op.create_table(
        "document_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # SET NULL preserves cleanup addresses after the document and file rows disappear.
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("storage_key", sa.String(82), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "extract",
                "delete_files",
                name="document_job_kind",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="document_job_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint(STORAGE_KEY_CHECK, name="ck_document_jobs_storage_key"),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts",
            name="ck_document_jobs_attempts",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL AND attempts >= 1) "
            "OR (state <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_document_jobs_lease",
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > started_at",
            name="ck_document_jobs_lease_expiry",
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL) "
            "OR (state IN ('queued', 'running') AND finished_at IS NULL)",
            name="ck_document_jobs_finished",
        ),
        sa.CheckConstraint(
            SAFE_CODE_CHECK.format(column="error_code"), name="ck_document_jobs_error_code"
        ),
    )
    op.create_index(
        "ix_document_jobs_claim", "document_jobs", ["kind", "state", "available_at", "created_at"]
    )
    op.create_index(
        "ix_document_jobs_expired_lease",
        "document_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index("ix_document_jobs_document", "document_jobs", ["document_id"])
    op.create_index(
        "uq_document_jobs_active_extract",
        "document_jobs",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'extract' AND state IN ('queued', 'running')"),
    )
    op.create_index(
        "uq_document_jobs_active_cleanup",
        "document_jobs",
        ["storage_key"],
        unique=True,
        postgresql_where=sa.text("kind = 'delete_files' AND state IN ('queued', 'running')"),
    )


def downgrade():
    # Check under locks so no concurrent insertion can race a destructive downgrade.
    op.execute(
        "LOCK TABLE document_files, document_sections, document_jobs IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM document_files)
           OR EXISTS (SELECT 1 FROM document_sections)
           OR EXISTS (SELECT 1 FROM document_jobs) THEN
            RAISE EXCEPTION 'Downgrade refused: document storage or processing data exists.';
        END IF;
    END $$""")
    op.drop_table("document_jobs")
    op.drop_table("document_sections")
    op.drop_table("document_files")
