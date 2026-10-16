"""Database representation; API request/response contracts live in schemas/."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, Index, String, Uuid, func
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
        Index("ix_documents_created_at_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
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
