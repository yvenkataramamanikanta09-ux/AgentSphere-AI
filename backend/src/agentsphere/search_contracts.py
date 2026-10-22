"""Versioned search data contracts only: no tokenizer, inference, worker or API I/O.

These types describe future search requests and verified results. They do not
authorize access or prove that a vector index has been published successfully.
"""

import hashlib
import json
import re
import unicodedata
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LOCAL_MODEL = "intfloat/multilingual-e5-small"
REVISION_PATTERN = r"^[0-9a-f]{40}$"
HASH_PATTERN = r"^[0-9a-f]{64}$"
CHUNK_ID_NAMESPACE = UUID("2208a5aa-4849-4e07-bae6-52364010e3e9")


def stable_chunk_id(
    *,
    document_id: UUID,
    source_fingerprint: str,
    profile_fingerprint: str,
    section_ordinal: int,
    char_start: int,
    char_end: int,
    content_sha256: str,
) -> UUID:
    """Name a logical chunk without tokenizing text or depending on section UUIDs.

    A different generation may store the same logical chunk under a different
    row/vector UUID. This helper defines only its document/source/profile/span
    identity. It neither verifies section content nor creates a chunk record.
    """
    hashes = (source_fingerprint, profile_fingerprint, content_sha256)
    if not isinstance(document_id, UUID) or any(
        not isinstance(value, str) or re.fullmatch(HASH_PATTERN, value) is None for value in hashes
    ):
        raise ValueError("Chunk identity requires a document UUID and lowercase SHA-256 hashes.")
    if (
        any(type(value) is not int for value in (section_ordinal, char_start, char_end))
        or section_ordinal < 0
        or char_start < 0
        or char_end <= char_start
    ):
        raise ValueError("Chunk identity requires an ordinal and a nonempty section-text range.")
    name = json.dumps(
        [str(document_id), *hashes, section_ordinal, char_start, char_end], separators=(",", ":")
    )
    return uuid5(CHUNK_ID_NAMESPACE, name)


class IndexState(StrEnum):
    BUILDING = "building"
    READY = "ready"
    EMPTY = "empty"
    FAILED = "failed"
    OBSOLETE = "obsolete"


class VectorJobKind(StrEnum):
    INDEX = "index"
    DELETE_GENERATION = "delete_generation"


class VectorJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


INDEX_TRANSITIONS = {
    IndexState.BUILDING: frozenset(
        {IndexState.READY, IndexState.EMPTY, IndexState.FAILED, IndexState.OBSOLETE}
    ),
    IndexState.READY: frozenset({IndexState.OBSOLETE}),
    IndexState.EMPTY: frozenset({IndexState.OBSOLETE}),
    IndexState.FAILED: frozenset({IndexState.OBSOLETE}),
    IndexState.OBSOLETE: frozenset(),
}
JOB_TRANSITIONS = {
    VectorJobState.QUEUED: frozenset({VectorJobState.RUNNING, VectorJobState.CANCELLED}),
    VectorJobState.RUNNING: frozenset(
        {
            VectorJobState.QUEUED,
            VectorJobState.SUCCEEDED,
            VectorJobState.FAILED,
            VectorJobState.CANCELLED,
        }
    ),
    VectorJobState.SUCCEEDED: frozenset(),
    VectorJobState.FAILED: frozenset(),
    VectorJobState.CANCELLED: frozenset(),
}


def validate_index_transition(current: IndexState, target: IndexState) -> None:
    """Validate a transition, not its future lease/source/publication preconditions.

    Retries build a new generation; ready/empty/failed payloads must never be rebuilt
    in place. A no-op is allowed so repeat observations remain idempotent.
    """
    current, target = IndexState(current), IndexState(target)
    if target != current and target not in INDEX_TRANSITIONS[current]:
        raise ValueError("Invalid indexing-generation transition.")


def validate_vector_job_transition(current: VectorJobState, target: VectorJobState) -> None:
    """Future workers must also apply ownership, expiry and attempt fencing."""
    current, target = VectorJobState(current), VectorJobState(target)
    if target != current and target not in JOB_TRANSITIONS[current]:
        raise ValueError("Invalid vector-job transition.")


class EmbeddingProfileSpec(BaseModel):
    """Immutable semantic identity; this does not fetch or load model artifacts.

    An explicit reviewed commit is mandatory for both model and tokenizer. The
    conservative token reserve is a contract bound, not a substitute for checking
    the exact prefixed tokenizer output in the later inference implementation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    profile_schema_version: Literal[1] = 1
    provider: Literal["local"] = "local"
    model_name: Literal[LOCAL_MODEL] = LOCAL_MODEL
    model_revision: str = Field(pattern=REVISION_PATTERN)
    tokenizer_name: Literal[LOCAL_MODEL] = LOCAL_MODEL
    tokenizer_revision: str = Field(pattern=REVISION_PATTERN)
    dimensions: Literal[384] = 384
    model_max_tokens: Literal[512] = 512
    chunk_tokens: int = Field(default=350, ge=1, le=480, strict=True)
    chunk_overlap_tokens: int = Field(default=50, ge=0, le=479, strict=True)
    normalize_embeddings: Literal[True] = True
    query_prefix: Literal["query: "] = "query: "
    passage_prefix: Literal["passage: "] = "passage: "
    chunker_version: str = Field(default="section_tokens_v1", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    distance_metric: Literal["cosine"] = "cosine"

    @model_validator(mode="after")
    def validate_token_budget(self):
        if self.chunk_overlap_tokens >= self.chunk_tokens:
            raise ValueError("Chunk overlap must be smaller than the chunk token target.")
        return self

    @property
    def fingerprint(self) -> str:
        """Hash explicit defaults too; field order or caller JSON order is irrelevant."""
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class SearchRequest(BaseModel):
    """Future POST contract; authorization and actual model-token checks come later."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=50, strict=True)
    include_archived: bool = False
    document_ids: tuple[UUID, ...] | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if not value or any(
            unicodedata.category(c) == "Cs" or (unicodedata.category(c) == "Cc" and c not in "\n\t")
            for c in value
        ):
            raise ValueError("Search query must contain text without control characters.")
        return value

    @field_validator("document_ids")
    @classmethod
    def unique_document_ids(cls, value: tuple[UUID, ...] | None):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Document filters must not contain duplicate identifiers.")
        return value


class SourceReference(BaseModel):
    """Locations copied from a verified section, never inferred from rendered text.

    Offsets use Python/Unicode code points into persisted section text, not UTF-8
    bytes or JavaScript UTF-16 units. Line ranges initially have section precision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    section_id: UUID
    section_ordinal: int = Field(ge=0, strict=True)
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(gt=0, strict=True)
    offset_unit: Literal["unicode_codepoints"] = "unicode_codepoints"
    page_number: int | None = Field(default=None, ge=1, strict=True)
    heading: str | None = Field(default=None, min_length=1, max_length=500)
    line_start: int | None = Field(default=None, ge=1, strict=True)
    line_end: int | None = Field(default=None, ge=1, strict=True)
    line_precision: Literal["section"] = "section"

    @model_validator(mode="after")
    def validate_locations(self):
        if self.char_end <= self.char_start:
            raise ValueError("Section-text offsets must form a nonempty half-open range.")
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("Source line bounds must be provided together.")
        if self.line_start is not None and self.line_end < self.line_start:
            raise ValueError("Source line bounds must be ordered.")
        if self.heading is not None and not self.heading.strip():
            raise ValueError("Source heading must contain text.")
        return self


class SearchResult(BaseModel):
    """A future API may construct this only after PostgreSQL reauthorization."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    document_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    chunk_id: UUID
    index_version_id: UUID
    text: str = Field(min_length=1, max_length=16_000)
    score: float = Field(ge=-1, le=1, allow_inf_nan=False)
    score_kind: Literal["cosine_similarity"] = "cosine_similarity"
    source: SourceReference


class SearchResponse(BaseModel):
    """Bounded top-K results, with no fabricated exact total or confidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    profile_id: UUID
    profile_fingerprint: str = Field(pattern=HASH_PATTERN)
    requested_limit: int = Field(ge=1, le=50, strict=True)
    results: tuple[SearchResult, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_limit(self):
        if len(self.results) > self.requested_limit:
            raise ValueError("Search results exceed the requested limit.")
        if len({result.chunk_id for result in self.results}) != len(self.results):
            raise ValueError("Search results must not contain duplicate chunks.")
        return self
