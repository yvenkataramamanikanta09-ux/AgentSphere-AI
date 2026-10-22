"""Bounded, source-preserving chunks over an explicitly pinned tokenizer.

Pure values only: callers must obtain a consistent, authorized extraction snapshot.
This module does not publish generations or fetch documents/model artifacts.
"""

import hashlib
import json
import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentsphere.search_contracts import (
    HASH_PATTERN,
    EmbeddingProfileSpec,
    SourceReference,
    stable_chunk_id,
)


class ChunkingError(ValueError):
    """Stable, content-free error; no section text is included in exceptions."""


class Tokenizer(Protocol):
    """The real adapter must use the profile's exact offline tokenizer snapshot."""

    profile: EmbeddingProfileSpec

    def count(self, text: str, *, add_special_tokens: bool = False) -> int: ...

    def offsets(self, text: str) -> Sequence[tuple[int, int]]: ...


class ExtractedSection(BaseModel):
    """Exact persisted section text and genuine section-level provenance."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid", hide_input_in_errors=True)

    document_id: UUID
    section_id: UUID
    ordinal: int = Field(ge=0)
    text: str = Field(repr=False)
    page_number: int | None = Field(default=None, ge=1)
    heading: str | None = Field(default=None, min_length=1, max_length=500)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_references(self):
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("Section line bounds must be provided together.")
        if self.line_start is not None and self.line_end < self.line_start:
            raise ValueError("Section line bounds must be ordered.")
        if self.heading is not None and not self.heading.strip():
            raise ValueError("Section heading must contain text.")
        return self


@dataclass(frozen=True)
class ChunkingLimits:
    max_sections: int = 10_000
    max_document_characters: int = 2_000_000
    max_section_tokens: int = 2_000_000
    max_chunks: int = 10_000
    max_chunk_characters: int = 16_000
    max_tokenizer_calls: int = 100_000
    timeout_seconds: float = 60.0

    def __post_init__(self):
        for value, maximum in (
            (self.max_sections, 10_000),
            (self.max_document_characters, 2_000_000),
            (self.max_section_tokens, 2_000_000),
            (self.max_chunks, 10_000),
            (self.max_chunk_characters, 16_000),
            (self.max_tokenizer_calls, 100_000),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Chunking resource limits exceed supported bounds.")
        if type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 120:
            raise ValueError("Chunking timeout must be positive and at most 120 seconds.")


@dataclass(frozen=True)
class TextChunk:
    logical_id: UUID
    ordinal: int
    text: str = field(repr=False)
    token_count: int
    input_token_count: int
    content_sha256: str
    embedding_input_sha256: str
    source: SourceReference


@dataclass(frozen=True)
class ChunkSet:
    document_id: UUID
    extraction_epoch: UUID
    source_fingerprint: str
    profile_fingerprint: str
    chunks: tuple[TextChunk, ...]


def _digest(value: str) -> str:
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise ChunkingError("invalid_unicode") from None


def _validate_sections(sections: Sequence[ExtractedSection], limits: ChunkingLimits) -> None:
    if not isinstance(sections, (tuple, list)) or len(sections) > limits.max_sections:
        raise ChunkingError("section_limit")
    if any(not isinstance(section, ExtractedSection) for section in sections):
        raise ChunkingError("invalid_section")
    if any(section.ordinal != ordinal for ordinal, section in enumerate(sections)):
        raise ChunkingError("section_order")
    if len({section.section_id for section in sections}) != len(sections):
        raise ChunkingError("duplicate_section")
    if sum(len(section.text) for section in sections) > limits.max_document_characters:
        raise ChunkingError("text_limit")
    if len({section.document_id for section in sections}) > 1:
        raise ChunkingError("mixed_documents")


def source_fingerprint(
    sections: Sequence[ExtractedSection],
    *,
    file_sha256: str,
    parser_version: str,
    limits: ChunkingLimits | None = None,
) -> str:
    """Canonical v1 identity excludes section UUIDs and extraction-attempt epochs.

    Text is hashed exactly, without Unicode normalization or whitespace changes.
    The SQL Stage 1 schema accepts this hash but does not compute this algorithm.
    """
    limits = limits or ChunkingLimits()
    _validate_sections(sections, limits)
    if not isinstance(file_sha256, str) or re.fullmatch(HASH_PATTERN, file_sha256) is None:
        raise ChunkingError("invalid_file_hash")
    if (
        not isinstance(parser_version, str)
        or not parser_version.strip()
        or len(parser_version) > 64
    ):
        raise ChunkingError("invalid_parser_version")
    manifest = {
        "source_schema_version": 1,
        "file_sha256": file_sha256,
        "parser_version": parser_version,
        "sections": [
            {
                "ordinal": section.ordinal,
                "text_sha256": _digest(section.text),
                "page_number": section.page_number,
                "heading": section.heading,
                "line_start": section.line_start,
                "line_end": section.line_end,
                "line_precision": "section",
            }
            for section in sections
        ],
    }
    return _digest(json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


class _Budget:
    def __init__(self, limits: ChunkingLimits):
        self.limits = limits
        self.deadline = monotonic() + limits.timeout_seconds
        self.calls = 0

    def check(self):
        if monotonic() >= self.deadline:
            raise ChunkingError("chunking_timeout")

    def count(self, tokenizer: Tokenizer, text: str, *, special: bool) -> int:
        self.check()
        self.calls += 1
        if self.calls > self.limits.max_tokenizer_calls:
            raise ChunkingError("tokenizer_work_limit")
        try:
            result = tokenizer.count(text, add_special_tokens=special)
        except Exception:
            raise ChunkingError("tokenization_failed") from None
        self.check()
        if type(result) is not int or result < 0:
            raise ChunkingError("invalid_token_count")
        return result


def _checked_offsets(tokenizer: Tokenizer, text: str, budget: _Budget):
    budget.check()
    budget.calls += 1
    if budget.calls > budget.limits.max_tokenizer_calls:
        raise ChunkingError("tokenizer_work_limit")
    try:
        offsets = tokenizer.offsets(text)
    except Exception:
        raise ChunkingError("tokenization_failed") from None
    budget.check()
    if not isinstance(offsets, (tuple, list)) or len(offsets) > budget.limits.max_section_tokens:
        raise ChunkingError("section_token_limit")
    previous_start = previous_end = 0
    for offset in offsets:
        if (
            not isinstance(offset, (tuple, list))
            or len(offset) != 2
            or any(type(value) is not int for value in offset)
        ):
            raise ChunkingError("invalid_token_offsets")
        start, end = offset
        if not (0 <= start < end <= len(text)) or start < previous_start or end < previous_end:
            raise ChunkingError("invalid_token_offsets")
        # Shared/overlapping spans occur for Unicode subword tokens; retain them.
        previous_start, previous_end = start, end
    return offsets


def chunk_document(
    *,
    document_id: UUID,
    extraction_epoch: UUID,
    file_sha256: str,
    parser_version: str,
    expected_source_fingerprint: str,
    sections: Sequence[ExtractedSection],
    tokenizer: Tokenizer,
    profile: EmbeddingProfileSpec | None = None,
    limits: ChunkingLimits | None = None,
) -> ChunkSet:
    """Return all verified chunks or raise; never publish a partial/truncated result.

    The 350/50 profile targets body tokens. Each exact substring is re-tokenized
    with and without the passage prefix/special tokens, so normalization effects
    cannot bypass the 512-token model budget. Timeout is cooperative between
    bounded tokenizer calls, not a claim of killable parsing/inference isolation.
    """
    limits = limits or ChunkingLimits()
    budget = _Budget(limits)
    profile = profile or tokenizer.profile
    if not isinstance(profile, EmbeddingProfileSpec) or not isinstance(
        tokenizer.profile, EmbeddingProfileSpec
    ):
        raise ChunkingError("invalid_profile")
    if profile.fingerprint != tokenizer.profile.fingerprint:
        raise ChunkingError("tokenizer_profile_mismatch")
    if profile.chunker_version != "section_tokens_v1":
        raise ChunkingError("unsupported_chunker_version")
    if not isinstance(document_id, UUID) or not isinstance(extraction_epoch, UUID):
        raise ChunkingError("invalid_source_identity")
    actual_fingerprint = source_fingerprint(
        sections, file_sha256=file_sha256, parser_version=parser_version, limits=limits
    )
    if actual_fingerprint != expected_source_fingerprint:
        raise ChunkingError("source_fingerprint_mismatch")
    if any(section.document_id != document_id for section in sections):
        raise ChunkingError("mixed_documents")
    chunks: list[TextChunk] = []
    for section in sections:
        budget.check()
        text = section.text
        if not text.strip():
            continue
        offsets = _checked_offsets(tokenizer, text, budget)
        if not offsets:
            raise ChunkingError("section_unencodable")
        ends = [end for _, end in offsets]
        boundaries = [
            match.end() for match in re.finditer(r"\n\n+|\n|[.!?\u0964\u0965](?:[ \t]+|$)", text)
        ]
        start = 0
        while start < len(text):
            budget.check()
            if len(chunks) >= limits.max_chunks:
                raise ChunkingError("chunk_limit")
            first = bisect_right(ends, start)
            last = min(first + profile.chunk_tokens, len(offsets))
            end = len(text) if last == len(offsets) else offsets[last - 1][1]
            end = min(end, start + limits.max_chunk_characters)
            # Prefer a real paragraph/sentence/line ending in the final quarter.
            boundary_index = bisect_right(boundaries, end) - 1
            if (
                end < len(text)
                and boundary_index >= 0
                and boundaries[boundary_index] >= start + (end - start) * 0.75
            ):
                end = boundaries[boundary_index]
            while end > start:
                body = text[start:end]
                count = budget.count(tokenizer, body, special=False)
                total = budget.count(tokenizer, profile.passage_prefix + body, special=True)
                if 0 < count <= profile.chunk_tokens and total <= profile.model_max_tokens:
                    break
                # Re-tokenized fragments can differ from the original whole-section
                # segmentation. Back off, checking actual counts each time.
                earlier = bisect_left(ends, end) - 1
                next_end = ends[earlier] if earlier >= 0 else start
                end = next_end if start < next_end < end else end - 1
            else:
                raise ChunkingError("section_unencodable")
            content_hash = _digest(body)
            reference = SourceReference(
                section_id=section.section_id,
                section_ordinal=section.ordinal,
                char_start=start,
                char_end=end,
                page_number=section.page_number,
                heading=section.heading,
                line_start=section.line_start,
                line_end=section.line_end,
            )
            chunks.append(
                TextChunk(
                    logical_id=stable_chunk_id(
                        document_id=document_id,
                        source_fingerprint=actual_fingerprint,
                        profile_fingerprint=profile.fingerprint,
                        section_ordinal=section.ordinal,
                        char_start=start,
                        char_end=end,
                        content_sha256=content_hash,
                    ),
                    ordinal=len(chunks),
                    text=body,
                    token_count=count,
                    input_token_count=total,
                    content_sha256=content_hash,
                    embedding_input_sha256=_digest(profile.passage_prefix + body),
                    source=reference,
                )
            )
            if end == len(text):
                break
            token_end = bisect_right(ends, end)
            next_index = max(first, token_end - profile.chunk_overlap_tokens)
            next_start = offsets[next_index][0] if next_index < len(offsets) else end
            # Repeated Unicode spans or tiny boundary fragments must still advance.
            if next_start <= start:
                next_start = next(
                    (
                        offsets[index][0]
                        for index in range(next_index, token_end)
                        if offsets[index][0] > start
                    ),
                    end,
                )
            start = min(next_start, end)
    budget.check()
    return ChunkSet(
        document_id=document_id,
        extraction_epoch=extraction_epoch,
        source_fingerprint=actual_fingerprint,
        profile_fingerprint=profile.fingerprint,
        chunks=tuple(chunks),
    )
