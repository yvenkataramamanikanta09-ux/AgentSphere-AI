"""Read and validate configuration once when an application is created."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from agentsphere.search_config import SearchSettings
from agentsphere.storage import validate_storage_root

# Resolve relative to this file so starting from another directory is safe.
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(SearchSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    max_upload_bytes: int = Field(default=10_000_000, gt=0, le=10_000_000)
    database_url: SecretStr | None = None
    test_database_url: SecretStr | None = None
    private_storage_root: Path = BACKEND_DIR / ".local" / "private_storage"
    upload_timeout_seconds: int = Field(default=120, ge=1, le=600)
    upload_concurrency: int = Field(default=2, ge=1, le=8)
    extraction_timeout_seconds: int = Field(default=60, ge=1, le=60)
    extraction_memory_mb: int = Field(default=512, ge=128, le=2048)
    extraction_max_pdf_pages: int = Field(default=500, ge=1, le=500)
    extraction_max_text_bytes: int = Field(default=2_000_000, ge=1024, le=2_000_000)
    extraction_max_sections: int = Field(default=5000, ge=1, le=5000)
    extraction_output_bytes: int = Field(default=20_000_000, ge=65_536, le=32_000_000)
    docx_max_entries: int = Field(default=1024, ge=3, le=1024)
    docx_max_expanded_bytes: int = Field(default=40_000_000, ge=1024, le=40_000_000)
    docx_max_entry_bytes: int = Field(default=20_000_000, ge=1024, le=20_000_000)
    docx_max_expansion_ratio: int = Field(default=100, ge=1, le=100)
    worker_lease_seconds: int = Field(default=120, ge=30, le=600)
    worker_retry_base_seconds: int = Field(default=2, ge=1, le=30)
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    reconciliation_min_age_seconds: int = Field(default=86_400, ge=3600)
    reconciliation_max_entries: int = Field(default=10_000, ge=1, le=100_000)
    environment: Literal["development", "production"] = "development"
    app_origin: str = "https://localhost:8443"
    allow_insecure_local_http: bool = False
    auth_rate_limit_key: SecretStr | None = None
    session_idle_seconds: int = Field(default=1800, ge=60)
    session_absolute_seconds: int = Field(default=43200, ge=60)
    anonymous_session_seconds: int = Field(default=600, ge=60)
    login_account_limit: int = Field(default=10, ge=1)
    login_ip_limit: int = Field(default=30, ge=1)
    registration_ip_limit: int = Field(default=5, ge=1)
    csrf_ip_limit: int = Field(default=60, ge=1)

    @field_validator("private_storage_root", mode="before")
    @classmethod
    def validate_private_storage(cls, value: str | Path) -> Path:
        return validate_storage_root(value)

    @model_validator(mode="after")
    def validate_security(self):
        if self.worker_lease_seconds < self.extraction_timeout_seconds + 15:
            raise ValueError(
                "Worker lease must exceed the extraction timeout by at least 15 seconds."
            )
        if self.extraction_output_bytes < (
            2 * self.extraction_max_text_bytes + self.extraction_max_sections * 2560 + 1024
        ):
            raise ValueError(
                "Extraction output limit must accommodate bounded text and references."
            )
        if self.docx_max_entry_bytes > self.docx_max_expanded_bytes:
            raise ValueError("DOCX entry limit cannot exceed the package expansion limit.")
        origin = urlsplit(self.app_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("APP_ORIGIN must be an origin without a path or credentials.")
        if self.allow_insecure_local_http:
            if self.environment != "development" or origin.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("Insecure HTTP is restricted to loopback development.")
        elif origin.scheme != "https":
            raise ValueError("HTTPS is required unless loopback HTTP is explicitly enabled.")
        if self.auth_rate_limit_key and len(self.auth_rate_limit_key.get_secret_value()) < 32:
            raise ValueError("AUTH_RATE_LIMIT_KEY must have at least 32 characters.")
        if self.environment == "production" and not self.auth_rate_limit_key:
            raise ValueError("Production requires AUTH_RATE_LIMIT_KEY.")
        return self

    @property
    def extraction_limits(self):
        # Import only for worker/extraction use, never parse files inside API handlers.
        from agentsphere.extraction import ExtractionLimits

        return ExtractionLimits(
            timeout_seconds=self.extraction_timeout_seconds,
            max_pdf_pages=self.extraction_max_pdf_pages,
            max_text_bytes=self.extraction_max_text_bytes,
            max_sections=self.extraction_max_sections,
            max_docx_entries=self.docx_max_entries,
            max_docx_expanded_bytes=self.docx_max_expanded_bytes,
            max_docx_entry_bytes=self.docx_max_entry_bytes,
            max_docx_ratio=self.docx_max_expansion_ratio,
        )

    @property
    def cookie_name(self) -> str:
        return (
            "agentsphere_dev_session"
            if self.allow_insecure_local_http
            else "__Host-agentsphere_session"
        )

    @field_validator("database_url", "test_database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            url = make_url(value.get_secret_value())
        except ArgumentError:
            raise ValueError("Use a valid PostgreSQL connection URL.") from None
        if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
            raise ValueError("Use postgresql+psycopg with an explicit host and database.")
        return value


def get_settings(request: Request) -> Settings:
    """Expose this app's settings as a FastAPI dependency."""
    return request.app.state.settings
