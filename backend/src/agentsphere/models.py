"""Database representation; API request/response contracts live in schemas/."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DocumentStatus(StrEnum):
    REGISTERED = "registered"
    ARCHIVED = "archived"


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="ck_documents_positive_size"),
        CheckConstraint("length(trim(filename)) > 0", name="ck_documents_filename"),
        CheckConstraint("length(trim(content_type)) > 0", name="ck_documents_content_type"),
        Index("ix_documents_workspace_created_id", "workspace_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            values_callable=lambda enum: [entry.value for entry in enum],
            native_enum=False,
            create_constraint=True,
            name="document_status",
        ),
        nullable=False,
        default=DocumentStatus.REGISTERED,
        server_default=DocumentStatus.REGISTERED.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuthSession(Base):
    __tablename__ = "sessions"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Membership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (CheckConstraint("role IN ('editor', 'viewer')", name="ck_membership_role"),)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RateLimitBucket(Base):
    __tablename__ = "auth_rate_limits"
    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    identity_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class ProcessingStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class JobKind(StrEnum):
    EXTRACT = "extract"
    DELETE_FILES = "delete_files"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Storage addresses contain only server-generated UUIDs, never display filenames.
STORAGE_KEY_CHECK = "storage_key ~ '^objects/[0-9a-f]{32}/[0-9a-f]{32}/original$'"
SAFE_CODE_CHECK = "{column} IS NULL OR {column} ~ '^[a-z][a-z0-9_]{{0,63}}$'"


class DocumentFile(Base):
    """One private original per document; lifecycle remains on Document.status."""

    __tablename__ = "document_files"
    __table_args__ = (
        CheckConstraint(STORAGE_KEY_CHECK, name="ck_document_files_storage_key"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_document_files_sha256"),
        CheckConstraint("length(trim(original_filename)) > 0", name="ck_document_files_filename"),
        CheckConstraint(
            SAFE_CODE_CHECK.format(column="error_code"), name="ck_document_files_error_code"
        ),
        CheckConstraint(
            SAFE_CODE_CHECK.format(column="warning_code"), name="ck_document_files_warning_code"
        ),
        CheckConstraint(
            "parser_version IS NULL OR length(trim(parser_version)) > 0",
            name="ck_document_files_parser_version",
        ),
        Index("ix_document_files_processing_uploaded", "processing_status", "uploaded_at"),
        Index("ix_document_files_uploaded_by", "uploaded_by"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    storage_key: Mapped[str] = mapped_column(String(82), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(
            ProcessingStatus,
            values_callable=lambda enum: [entry.value for entry in enum],
            native_enum=False,
            create_constraint=True,
            name="document_processing_status",
        ),
        nullable=False,
        default=ProcessingStatus.QUEUED,
        server_default=ProcessingStatus.QUEUED.value,
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    warning_code: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str | None] = mapped_column(String(64))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentSection(Base):
    """Extracted text in source order, without inventing page information."""

    __tablename__ = "document_sections"
    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="ck_document_sections_ordinal"),
        CheckConstraint("length(text) > 0", name="ck_document_sections_text"),
        CheckConstraint(
            "page_number IS NULL OR page_number >= 1", name="ck_document_sections_page"
        ),
        CheckConstraint(
            "(line_start IS NULL AND line_end IS NULL) OR "
            "(line_start IS NOT NULL AND line_end IS NOT NULL "
            "AND line_start >= 1 AND line_end >= line_start)",
            name="ck_document_sections_lines",
        ),
        CheckConstraint(
            "heading IS NULL OR length(trim(heading)) > 0", name="ck_document_sections_heading"
        ),
        Index("uq_document_sections_document_ordinal", "document_id", "ordinal", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(500))
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)


class DocumentJob(Base):
    """Durable work addresses survive deletion of their document or attachment."""

    __tablename__ = "document_jobs"
    __table_args__ = (
        CheckConstraint(STORAGE_KEY_CHECK, name="ck_document_jobs_storage_key"),
        CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1 AND attempts <= max_attempts",
            name="ck_document_jobs_attempts",
        ),
        CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL AND attempts >= 1) "
            "OR (state <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_document_jobs_lease",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > started_at",
            name="ck_document_jobs_lease_expiry",
        ),
        CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled') AND finished_at IS NOT NULL) "
            "OR (state IN ('queued', 'running') AND finished_at IS NULL)",
            name="ck_document_jobs_finished",
        ),
        CheckConstraint(
            SAFE_CODE_CHECK.format(column="error_code"), name="ck_document_jobs_error_code"
        ),
        Index("ix_document_jobs_claim", "kind", "state", "available_at", "created_at"),
        Index(
            "ix_document_jobs_expired_lease",
            "lease_expires_at",
            postgresql_where=text("state = 'running'"),
        ),
        Index("ix_document_jobs_document", "document_id"),
        Index(
            "uq_document_jobs_active_extract",
            "document_id",
            unique=True,
            postgresql_where=text("kind = 'extract' AND state IN ('queued', 'running')"),
        ),
        Index(
            "uq_document_jobs_active_cleanup",
            "storage_key",
            unique=True,
            postgresql_where=text("kind = 'delete_files' AND state IN ('queued', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    storage_key: Mapped[str] = mapped_column(String(82), nullable=False)
    kind: Mapped[JobKind] = mapped_column(
        Enum(
            JobKind,
            values_callable=lambda enum: [entry.value for entry in enum],
            native_enum=False,
            create_constraint=True,
            name="document_job_kind",
        ),
        nullable=False,
    )
    state: Mapped[JobState] = mapped_column(
        Enum(
            JobState,
            values_callable=lambda enum: [entry.value for entry in enum],
            native_enum=False,
            create_constraint=True,
            name="document_job_state",
        ),
        nullable=False,
        default=JobState.QUEUED,
        server_default=JobState.QUEUED.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
