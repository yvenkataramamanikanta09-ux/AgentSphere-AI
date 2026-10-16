"""Read and validate configuration once when an application is created."""

from pathlib import Path

from fastapi import Request
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

# Resolve relative to this file so starting from another directory is safe.
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    max_upload_bytes: int = Field(default=10_000_000, gt=0)
    database_url: SecretStr | None = None
    test_database_url: SecretStr | None = None

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
