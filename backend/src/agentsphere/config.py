"""Read and validate configuration once when an application is created."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import Field, SecretStr, field_validator, model_validator
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

    @model_validator(mode="after")
    def validate_security(self):
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
