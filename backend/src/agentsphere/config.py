"""Read and validate configuration once when an application is created."""

from pathlib import Path

from fastapi import Request
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve relative to this file so starting from another directory is safe.
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_upload_bytes: int = Field(default=10_000_000, gt=0)


def get_settings(request: Request) -> Settings:
    """Expose this app's settings as a FastAPI dependency."""
    return request.app.state.settings
