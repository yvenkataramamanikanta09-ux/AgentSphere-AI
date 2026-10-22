"""Module 6 metadata only: no inference, network clients, workers, or startup DDL.

PostgreSQL owns visibility. A future Qdrant collection is only a derived index.
Revision 0004 adds guards for cross-row invariants as well as these constraints.
"""

from datetime import datetime
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
from sqlalchemy.orm import Mapped, mapped_column

from agentsphere.models import Base
from agentsphere.search_contracts import IndexState, VectorJobKind, VectorJobState


def _enum(enum, name):
    return Enum(
        enum,
        values_callable=lambda values: [entry.value for entry in values],
        native_enum=False,
        create_constraint=True,
        name=name,
    )


class EmbeddingProfile(Base):
    """Immutable configuration, not model weights or service credentials."""

    __tablename__ = "embedding_profiles"
    __table_args__ = (
        CheckConstraint("fingerprint ~ '^[0-9a-f]{64}$'", name="ck_embedding_profiles_hash"),
        CheckConstraint("provider = 'local'", name="ck_embedding_profiles_local"),
        CheckConstraint(
            "model_name = 'intfloat/multilingual-e5-small' AND tokenizer_name = model_name",
            name="ck_embedding_profiles_model",
        ),
        CheckConstraint(
            "model_revision ~ '^[0-9a-f]{40}$' AND tokenizer_revision ~ '^[0-9a-f]{40}$'",
            name="ck_embedding_profiles_revisions",
        ),
        CheckConstraint(
            "dimensions = 384 AND model_max_tokens = 512 AND distance_metric = 'cosine' "
            "AND normalize_embeddings",
            name="ck_embedding_profiles_vector",
        ),
        CheckConstraint(
            "chunk_tokens BETWEEN 1 AND 480 AND chunk_overlap_tokens >= 0 "
            "AND chunk_overlap_tokens < chunk_tokens",
            name="ck_embedding_profiles_chunking",
        ),
        CheckConstraint(
            "query_prefix = 'query: ' AND passage_prefix = 'passage: '",
            name="ck_embedding_profiles_prefixes",
        ),
        CheckConstraint(
            "chunker_version ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="ck_embedding_profiles_chunker",
        ),
        CheckConstraint(
            "collection_name = 'as_' || fingerprint", name="ck_embedding_profiles_collection"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    tokenizer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    tokenizer_revision: Mapped[str] = mapped_column(String(40), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_overlap_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    model_max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    normalize_embeddings: Mapped[bool] = mapped_column(Boolean, nullable=False)
    query_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    passage_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    distance_metric: Mapped[str] = mapped_column(String(16), nullable=False)
    collection_name: Mapped[str] = mapped_column(String(67), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentSearchSource(Base):
    """One current extraction snapshot. Document ownership stays on documents."""

    __tablename__ = "document_search_sources"
    __table_args__ = (
        CheckConstraint(
            "source_fingerprint ~ '^[0-9a-f]{64}$' AND file_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_document_search_sources_hashes",
        ),
        CheckConstraint(
            "length(trim(parser_version)) > 0", name="ck_document_search_sources_parser"
        ),
    )

    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    file_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_files.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    extraction_epoch: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DocumentIndexVersion(Base):
    """A claim-attempt generation; readiness and activation require database guards."""

    __tablename__ = "document_index_versions"
    __table_args__ = (
        CheckConstraint(
            "source_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_document_index_versions_source"
        ),
        CheckConstraint(
            "manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_document_index_versions_manifest",
        ),
        CheckConstraint(
            "expected_chunk_count BETWEEN 0 AND 10000", name="ck_document_index_versions_count"
        ),
        CheckConstraint(
            "NOT is_active OR state = 'ready'", name="ck_document_index_versions_active"
        ),
        CheckConstraint("claim_fence >= 1", name="ck_document_index_versions_fence"),
        CheckConstraint(
            "(state = 'building' AND completed_at IS NULL AND verified_at IS NULL) OR "
            "(state IN ('ready', 'empty') AND completed_at IS NOT NULL "
            "AND verified_at IS NOT NULL AND manifest_sha256 IS NOT NULL) OR "
            "(state IN ('failed', 'obsolete') AND completed_at IS NOT NULL)",
            name="ck_document_index_versions_completion",
        ),
        CheckConstraint(
            "(state <> 'ready' OR expected_chunk_count > 0) "
            "AND (state <> 'empty' OR expected_chunk_count = 0)",
            name="ck_document_index_versions_empty",
        ),
        Index("ix_document_index_versions_profile_state", "profile_id", "state", "created_at"),
        Index(
            "uq_document_index_versions_active",
            "document_id",
            "profile_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_document_index_versions_document", "document_id", "extraction_epoch"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_search_sources.document_id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("embedding_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    extraction_epoch: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_token: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    claim_fence: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    state: Mapped[IndexState] = mapped_column(
        _enum(IndexState, "document_index_state"),
        nullable=False,
        default=IndexState.BUILDING,
        server_default="building",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    expected_chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DocumentChunk(Base):
    """Generation-local row plus stable logical identity and honest source locations."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0 AND section_ordinal >= 0", name="ck_document_chunks_ordinals"
        ),
        CheckConstraint("length(trim(text)) > 0", name="ck_document_chunks_text"),
        CheckConstraint(
            "content_sha256 = encode(sha256(convert_to(text, 'UTF8')), 'hex')",
            name="ck_document_chunks_content_hash",
        ),
        CheckConstraint(
            "embedding_input_sha256 ~ '^[0-9a-f]{64}$'", name="ck_document_chunks_input_hash"
        ),
        CheckConstraint("token_count BETWEEN 1 AND 480", name="ck_document_chunks_tokens"),
        CheckConstraint(
            "char_start >= 0 AND char_end > char_start AND char_end - char_start = length(text)",
            name="ck_document_chunks_span",
        ),
        CheckConstraint("page_number IS NULL OR page_number >= 1", name="ck_document_chunks_page"),
        CheckConstraint(
            "(line_start IS NULL AND line_end IS NULL) OR "
            "(line_start IS NOT NULL AND line_end IS NOT NULL "
            "AND line_start >= 1 AND line_end >= line_start)",
            name="ck_document_chunks_lines",
        ),
        CheckConstraint(
            "heading IS NULL OR length(trim(heading)) > 0", name="ck_document_chunks_heading"
        ),
        CheckConstraint("source_precision = 'section'", name="ck_document_chunks_precision"),
        Index("uq_document_chunks_generation_ordinal", "generation_id", "ordinal", unique=True),
        Index("uq_document_chunks_generation_logical", "generation_id", "logical_id", unique=True),
        Index("ix_document_chunks_section", "section_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    generation_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "document_index_versions.id", ondelete="CASCADE", deferrable=True, initially="DEFERRED"
        ),
        nullable=False,
    )
    logical_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    section_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_sections.id", ondelete="SET NULL")
    )
    section_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(500))
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    source_precision: Mapped[str] = mapped_column(
        String(16), nullable=False, default="section", server_default="section"
    )


class VectorJob(Base):
    """Outbox addresses survive every source FK being nulled by a deletion.

    Snapshot identifiers, not nullable live links, are the cleanup address.
    No consumer or automatic enqueue hook is enabled in Stage 1.
    """

    __tablename__ = "vector_jobs"
    __table_args__ = (
        CheckConstraint(
            "collection_name = 'as_' || profile_fingerprint "
            "AND profile_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND source_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_vector_jobs_scope",
        ),
        CheckConstraint(
            "document_id IS NULL OR document_id = target_document_id",
            name="ck_vector_jobs_document",
        ),
        CheckConstraint(
            "profile_id IS NULL OR profile_id = target_profile_id", name="ck_vector_jobs_profile"
        ),
        CheckConstraint(
            "generation_id IS NULL OR "
            "(target_generation_id IS NOT NULL AND generation_id = target_generation_id)",
            name="ck_vector_jobs_generation",
        ),
        CheckConstraint(
            "kind <> 'delete_generation' OR target_generation_id IS NOT NULL",
            name="ck_vector_jobs_cleanup_target",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts BETWEEN 1 AND 10 AND attempts <= max_attempts "
            "AND lease_generation >= attempts",
            name="ck_vector_jobs_attempts",
        ),
        CheckConstraint(
            "(state = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND started_at IS NOT NULL AND attempts >= 1) OR "
            "(state <> 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_vector_jobs_lease",
        ),
        CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > started_at",
            name="ck_vector_jobs_expiry",
        ),
        CheckConstraint(
            "(state IN ('succeeded','failed','cancelled') AND finished_at IS NOT NULL) OR "
            "(state IN ('queued','running') AND finished_at IS NULL)",
            name="ck_vector_jobs_finished",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="ck_vector_jobs_error",
        ),
        Index("ix_vector_jobs_claim", "kind", "state", "available_at", "created_at"),
        Index(
            "ix_vector_jobs_expired", "lease_expires_at", postgresql_where=text("state = 'running'")
        ),
        Index("ix_vector_jobs_document", "document_id"),
        Index("ix_vector_jobs_profile", "profile_id"),
        Index("ix_vector_jobs_generation", "generation_id"),
        Index(
            "uq_vector_jobs_active_index",
            "target_document_id",
            "target_profile_id",
            "extraction_epoch",
            unique=True,
            postgresql_where=text("kind = 'index' AND state IN ('queued','running')"),
        ),
        Index(
            "uq_vector_jobs_cleanup_tombstone",
            "collection_name",
            "target_generation_id",
            unique=True,
            postgresql_where=text("kind = 'delete_generation'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[VectorJobKind] = mapped_column(
        _enum(VectorJobKind, "vector_job_kind"), nullable=False
    )
    state: Mapped[VectorJobState] = mapped_column(
        _enum(VectorJobState, "vector_job_state"),
        nullable=False,
        default=VectorJobState.QUEUED,
        server_default="queued",
    )
    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    profile_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("embedding_profiles.id", ondelete="SET NULL")
    )
    generation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "document_index_versions.id", ondelete="SET NULL", deferrable=True, initially="DEFERRED"
        )
    )
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    target_document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    target_profile_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    target_generation_id: Mapped[UUID | None] = mapped_column(Uuid)
    extraction_epoch: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    collection_name: Mapped[str] = mapped_column(String(67), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    lease_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
