"""Metadata checks do not prove the type or safety of actual file contents."""

from datetime import datetime
from pathlib import PurePath
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentsphere.models import DocumentStatus, ProcessingStatus

SUPPORTED_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}
WINDOWS_DEVICE_NAMES = (
    RESERVED_NAMES
    | {"CONIN$", "CONOUT$"}
    | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "\u00b9\u00b2\u00b3"}
)


class DocumentMetadataFields(BaseModel):
    """Shared fields retain the validation policy used by existing stored records."""

    model_config = ConfigDict(extra="forbid")

    filename: str = Field(strict=True, min_length=1, max_length=255, examples=["proposal.pdf"])
    content_type: str = Field(strict=True, min_length=1, max_length=255)
    size_bytes: int = Field(strict=True, gt=0, description="Declared file size in bytes.")

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if value != value.strip() or value.startswith(".") or value.endswith("."):
            raise ValueError(
                "Use a filename without surrounding whitespace or leading/trailing dots."
            )
        if any(char in '<>:"/\\|?*' or not char.isprintable() for char in value):
            raise ValueError(
                "Use a filename, not a path; control and reserved characters are invalid."
            )
        if value.split(".", 1)[0].upper() in RESERVED_NAMES:
            raise ValueError("Reserved device names are not valid filenames.")
        if PurePath(value).suffix.lower() not in SUPPORTED_CONTENT_TYPES:
            raise ValueError("Supported extensions are .pdf, .docx, .txt, .md, and .markdown.")
        return value

    @field_validator("content_type")
    @classmethod
    def normalize_content_type(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_content_type(self) -> Self:
        extension = PurePath(self.filename).suffix.lower()
        expected = SUPPORTED_CONTENT_TYPES[extension]
        if self.content_type != expected:
            raise ValueError(f"The declared content_type for {extension} must be {expected}.")
        return self


class DocumentMetadata(DocumentMetadataFields):
    """New filenames must also avoid Windows device aliases in downloads."""

    @field_validator("filename")
    @classmethod
    def validate_portable_filename(cls, value: str) -> str:
        # Windows recognizes spaces before the first dot and superscript port
        # numbers as device aliases. Private object paths remain UUIDs regardless.
        if value.split(".", 1)[0].rstrip(" ").upper() in WINDOWS_DEVICE_NAMES:
            raise ValueError("Reserved device names are not valid filenames.")
        return value


class DocumentValidationResponse(BaseModel):
    valid: Literal[True] = True
    document: DocumentMetadata
    max_upload_bytes: int


class DocumentRead(DocumentMetadataFields):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime


class DocumentUpdate(BaseModel):
    """PATCH fields are optional, but explicitly supplying null is invalid."""

    model_config = ConfigDict(extra="forbid")

    filename: str | None = Field(default=None, strict=True, min_length=1, max_length=255)
    content_type: str | None = Field(default=None, strict=True, min_length=1, max_length=255)
    size_bytes: int | None = Field(default=None, strict=True, gt=0)
    status: DocumentStatus | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one field to update.")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Updated fields cannot be null.")
        return self


class DocumentPage(BaseModel):
    items: list[DocumentRead]
    limit: int
    offset: int
    has_more: bool


class FileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    original_filename: str
    sha256: str
    uploaded_by: UUID
    uploaded_at: datetime
    processing_status: ProcessingStatus
    processed_at: datetime | None
    error_code: str | None
    warning_code: str | None
    download_available: bool = False


class UploadRead(BaseModel):
    document: DocumentRead
    file: FileRead
    replayed: bool = False


class SectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    ordinal: int
    text: str
    page_number: int | None
    heading: str | None
    line_start: int | None
    line_end: int | None


class SectionPage(BaseModel):
    document_id: UUID
    items: list[SectionRead]
    limit: int
    offset: int
    has_more: bool
