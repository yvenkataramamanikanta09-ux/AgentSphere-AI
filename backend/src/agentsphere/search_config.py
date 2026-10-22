"""Inactive Module 6 settings; validating configuration performs no service/file I/O."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentsphere.search_contracts import LOCAL_MODEL, REVISION_PATTERN, EmbeddingProfileSpec

DEFAULT_MODEL_CACHE = Path(__file__).resolve().parents[2] / ".local" / "models"


class SearchSettings(BaseSettings):
    """Explicit flat search_* names map directly to SEARCH_* environment values.

    Settings inherits these fields so its existing _env_file argument and source
    precedence continue to apply once, rather than a nested factory rereading .env.
    """

    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    search_enabled: bool = False
    search_indexing_enabled: bool = False
    search_hosted_embeddings_enabled: bool = False
    search_embedding_provider: Literal["local"] = "local"
    search_model_name: Literal[LOCAL_MODEL] = LOCAL_MODEL
    search_model_revision: str | None = Field(default=None, pattern=REVISION_PATTERN)
    search_tokenizer_name: Literal[LOCAL_MODEL] = LOCAL_MODEL
    search_tokenizer_revision: str | None = Field(default=None, pattern=REVISION_PATTERN)
    search_embedding_dimensions: int = Field(default=384, ge=384, le=384)
    search_model_cache: Path = DEFAULT_MODEL_CACHE
    search_offline_models: bool = True
    search_trust_remote_code: bool = False
    search_chunk_tokens: int = Field(default=350, ge=1, le=480)
    search_chunk_overlap_tokens: int = Field(default=50, ge=0, le=479)
    search_qdrant_url: str | None = None
    search_qdrant_allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1", "qdrant")
    search_allow_insecure_local_qdrant: bool = False
    search_qdrant_read_key: SecretStr | None = None
    search_qdrant_write_key: SecretStr | None = None
    search_qdrant_verify_tls: bool = True
    search_collection_prefix: Literal["as"] = "as"
    search_batch_size: int = Field(default=32, ge=1, le=128)
    search_max_chunks_per_document: int = Field(default=10_000, ge=1, le=10_000)
    search_embedding_timeout_seconds: int = Field(default=60, ge=1, le=120)
    search_qdrant_timeout_seconds: int = Field(default=10, ge=1, le=30)
    search_worker_lease_seconds: int = Field(default=180, ge=30, le=600)
    search_worker_max_attempts: int = Field(default=3, ge=1, le=5)
    search_worker_retry_base_seconds: int = Field(default=2, ge=1, le=30)
    search_worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    search_inference_concurrency: int = Field(default=1, ge=1, le=4)
    search_query_max_tokens: int = Field(default=256, ge=1, le=256)
    search_query_max_characters: int = Field(default=2000, ge=1, le=2000)
    search_query_max_body_bytes: int = Field(default=16_384, ge=1024, le=16_384)
    search_query_max_results: int = Field(default=50, ge=1, le=50)
    search_query_requests_per_minute: int = Field(default=30, ge=1, le=120)
    search_query_timeout_seconds: int = Field(default=15, ge=1, le=60)
    search_reconciliation_min_age_seconds: int = Field(default=86_400, ge=3600)
    search_reconciliation_max_entries: int = Field(default=10_000, ge=1, le=100_000)

    @field_validator("search_model_cache", mode="before")
    @classmethod
    def validate_model_cache(cls, value):
        raw = str(value)
        path = Path(value)
        if (
            not path.is_absolute()
            or raw.startswith(("\\\\", "//"))
            or ".." in path.parts
            or "\x00" in raw
            or path == Path(path.anchor)
        ):
            raise ValueError("Model cache must be an absolute, non-root local directory path.")
        return path

    @field_validator("search_qdrant_read_key", "search_qdrant_write_key")
    @classmethod
    def validate_key(cls, value: SecretStr | None):
        if value is not None:
            secret = value.get_secret_value()
            if len(secret) < 32 or any(ord(char) < 33 or ord(char) > 126 for char in secret):
                raise ValueError(
                    "Qdrant keys must contain at least 32 printable nonspace characters."
                )
        return value

    @field_validator("search_qdrant_allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, value: tuple[str, ...]):
        if not value or any(
            not host or host != host.lower() or any(c in host for c in "/@?#*\\ ") for host in value
        ):
            raise ValueError("Qdrant host allowlist must contain explicit lowercase hostnames.")
        if len(value) != len(set(value)):
            raise ValueError("Qdrant host allowlist must not contain duplicates.")
        return value

    @model_validator(mode="after")
    def validate_search_security(self):
        if self.search_hosted_embeddings_enabled:
            raise ValueError("Hosted embeddings are not permitted in this implementation stage.")
        if not self.search_offline_models or self.search_trust_remote_code:
            raise ValueError("Search requires offline model use without remote model code.")
        if not self.search_qdrant_verify_tls:
            raise ValueError("Qdrant TLS verification cannot be disabled.")
        if self.search_chunk_overlap_tokens >= self.search_chunk_tokens:
            raise ValueError("Search chunk overlap must be smaller than the chunk token target.")
        if self.search_worker_lease_seconds < (
            self.search_embedding_timeout_seconds + self.search_qdrant_timeout_seconds + 15
        ):
            raise ValueError("Search worker lease must exceed bounded batch work by 15 seconds.")
        production = getattr(self, "environment", "development") == "production"
        if production and self.search_allow_insecure_local_qdrant:
            raise ValueError("Insecure Qdrant HTTP is restricted to development.")
        if self.search_qdrant_url is not None:
            try:
                url = urlsplit(self.search_qdrant_url)
                port = url.port
            except ValueError:
                raise ValueError("Qdrant URL must be a valid private service origin.") from None
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.hostname not in self.search_qdrant_allowed_hosts
                or url.username is not None
                or url.password is not None
                or url.path
                or url.query
                or url.fragment
                or (port is not None and not 1 <= port <= 65535)
                or any(ord(char) < 33 for char in self.search_qdrant_url)
            ):
                raise ValueError(
                    "Qdrant URL must be an allowed origin without credentials or paths."
                )
            if url.scheme == "http" and (
                production
                or not self.search_allow_insecure_local_qdrant
                or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError("Qdrant HTTP requires explicit loopback development permission.")
        enabled = self.search_enabled or self.search_indexing_enabled
        if enabled:
            if self.search_model_revision is None or self.search_tokenizer_revision is None:
                raise ValueError("Enabled search requires pinned model and tokenizer revisions.")
            if self.search_qdrant_url is None:
                raise ValueError("Enabled search requires an explicit private Qdrant origin.")
            if self.search_qdrant_read_key is None:
                raise ValueError("Enabled search requires a Qdrant read key.")
            if self.search_indexing_enabled and self.search_qdrant_write_key is None:
                raise ValueError("Enabled indexing requires a separate Qdrant write key.")
        if (
            self.search_qdrant_read_key is not None
            and self.search_qdrant_write_key is not None
            and self.search_qdrant_read_key == self.search_qdrant_write_key
        ):
            raise ValueError("Qdrant read and write keys must be distinct.")
        return self

    @property
    def search_profile(self) -> EmbeddingProfileSpec:
        """Return a contract only; callers cannot accidentally download anything."""
        if self.search_model_revision is None or self.search_tokenizer_revision is None:
            raise ValueError("A search profile requires pinned model and tokenizer revisions.")
        return EmbeddingProfileSpec(
            model_name=self.search_model_name,
            model_revision=self.search_model_revision,
            tokenizer_name=self.search_tokenizer_name,
            tokenizer_revision=self.search_tokenizer_revision,
            dimensions=self.search_embedding_dimensions,
            chunk_tokens=self.search_chunk_tokens,
            chunk_overlap_tokens=self.search_chunk_overlap_tokens,
        )
