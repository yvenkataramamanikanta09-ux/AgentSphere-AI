"""Chunking invariants with deliberately synthetic tokenizers, not retrieval evidence."""

import hashlib
import re
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentsphere.chunking import (
    ChunkingError,
    ChunkingLimits,
    ExtractedSection,
    chunk_document,
    source_fingerprint,
)
from agentsphere.search_contracts import EmbeddingProfileSpec


class CharacterTokenizer:
    """Testing only: one Unicode code point per body token."""

    def __init__(self, profile=None):
        self.profile = profile or EmbeddingProfileSpec(
            model_revision="1" * 40, tokenizer_revision="1" * 40
        )

    def count(self, text, *, add_special_tokens=False):
        return len(text) + (2 if add_special_tokens else 0)

    def offsets(self, text):
        return tuple((index, index + 1) for index in range(len(text)))


@pytest.fixture
def tokenizer():
    return CharacterTokenizer()


@pytest.fixture
def section():
    return ExtractedSection(
        document_id=uuid4(), section_id=uuid4(), ordinal=0, text="Synthetic source text."
    )


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run(sections, tokenizer, **kwargs):
    fingerprint = source_fingerprint(
        sections,
        file_sha256="f" * 64,
        parser_version="synthetic_v1",
        limits=kwargs.get("limits"),
    )
    arguments = dict(
        document_id=sections[0].document_id if sections else uuid4(),
        extraction_epoch=uuid4(),
        file_sha256="f" * 64,
        parser_version="synthetic_v1",
        expected_source_fingerprint=fingerprint,
        sections=sections,
        tokenizer=tokenizer,
    )
    arguments.update(kwargs)
    return chunk_document(**arguments)


def assert_exact_coverage(sections, chunks):
    for section in sections:
        spans = sorted(
            (chunk.source.char_start, chunk.source.char_end)
            for chunk in chunks
            if chunk.source.section_id == section.section_id
        )
        if not section.text.strip():
            assert spans == []
            continue
        cursor = 0
        for start, end in spans:
            assert start <= cursor
            cursor = max(cursor, end)
        assert cursor == len(section.text)
    for chunk in chunks:
        section = next(s for s in sections if s.section_id == chunk.source.section_id)
        assert chunk.text == section.text[chunk.source.char_start : chunk.source.char_end]
        assert chunk.content_sha256 == digest(chunk.text)
        assert chunk.embedding_input_sha256 == digest("passage: " + chunk.text)
        assert 0 < chunk.token_count <= 350
        assert 0 < chunk.input_token_count <= 512


def test_default_window_target_and_exact_overlap(tokenizer, section):
    section = section.model_copy(update={"text": "x" * 1000})
    chunks = run([section], tokenizer).chunks
    assert [(c.source.char_start, c.source.char_end) for c in chunks] == [
        (0, 350),
        (300, 650),
        (600, 950),
        (900, 1000),
    ]
    assert_exact_coverage([section], chunks)


@pytest.mark.parametrize(
    "text",
    [
        "తెలుగు భాషలో విజ్ఞానం. " * 80,
        "हिन्दी में विज्ञान। नई जानकारी। " * 80,
        "English తెలుగు हिन्दी 👩🏽‍💻 e\u0301 中文\n" * 80,
        "\n  leading spaces\tand trailing whitespace  \n" * 50,
        "a" * 1000,
        " \t\n",
        "",
        "😀",
        "\u200bcontrol\u0000text",
    ],
)
def test_unicode_substrings_never_normalized_or_lost(tokenizer, section, text):
    section = section.model_copy(update={"text": text})
    chunks = run([section], tokenizer).chunks
    assert_exact_coverage([section], chunks)
    assert len({chunk.logical_id for chunk in chunks}) == len(chunks)


@pytest.mark.parametrize(
    "location",
    [
        {"page_number": 7},
        {"heading": "Original DOCX heading"},
        {"line_start": 21, "line_end": 38},
        {"heading": "Markdown heading", "line_start": 42, "line_end": 60},
        {},
    ],
)
def test_only_original_source_locations_are_retained(tokenizer, section, location):
    section = section.model_copy(update={"text": "x" * 800, **location})
    chunks = run([section], tokenizer).chunks
    for chunk in chunks:
        for key in ("page_number", "heading", "line_start", "line_end"):
            assert getattr(chunk.source, key) == location.get(key)
        assert chunk.source.line_precision == "section"
        assert chunk.source.offset_unit == "unicode_codepoints"
    assert_exact_coverage([section], chunks)


def test_chunks_never_cross_sections(tokenizer, section):
    sections = [
        section.model_copy(update={"text": "a" * 500}),
        section.model_copy(update={"section_id": uuid4(), "ordinal": 1, "text": "b" * 500}),
    ]
    result = run(sections, tokenizer)
    assert all(len(set(c.text)) == 1 for c in result.chunks)
    assert [c.ordinal for c in result.chunks] == list(range(len(result.chunks)))
    assert_exact_coverage(sections, result.chunks)


def test_stable_logical_ids_survive_reextraction_uuid_changes(tokenizer, section):
    first = run([section], tokenizer)
    second = run([section.model_copy(update={"section_id": uuid4()})], tokenizer)
    assert first.extraction_epoch != second.extraction_epoch
    assert first.source_fingerprint == second.source_fingerprint
    assert first.chunks[0].logical_id == second.chunks[0].logical_id
    assert first.chunks[0].source.section_id != second.chunks[0].source.section_id


@pytest.mark.parametrize(
    "change",
    [
        {"text": "Changed exact text"},
        {"page_number": 3},
        {"heading": "Heading"},
        {"line_start": 1, "line_end": 2},
    ],
)
def test_source_content_or_reference_changes_identity(tokenizer, section, change):
    first = run([section], tokenizer)
    second = run([section.model_copy(update=change)], tokenizer)
    assert first.source_fingerprint != second.source_fingerprint
    assert first.chunks[0].logical_id != second.chunks[0].logical_id


def test_changed_profile_changes_logical_identity(tokenizer, section):
    first = run([section], tokenizer)
    tokenizer.profile = tokenizer.profile.model_copy(update={"model_revision": "2" * 40})
    second = run([section], tokenizer)
    assert first.source_fingerprint == second.source_fingerprint
    assert first.chunks[0].logical_id != second.chunks[0].logical_id


def test_stale_expected_fingerprint_is_rejected(tokenizer, section):
    with pytest.raises(ChunkingError, match="source_fingerprint_mismatch"):
        run([section], tokenizer, expected_source_fingerprint="0" * 64)


def test_exact_prefixed_input_budget_rechecked(tokenizer, section):
    class ExpensivePrefix(CharacterTokenizer):
        def count(self, text, *, add_special_tokens=False):
            return len(text) + (300 if add_special_tokens else 0)

    section = section.model_copy(update={"text": "x" * 700})
    chunks = run([section], ExpensivePrefix()).chunks
    assert all(c.token_count <= 203 and c.input_token_count <= 512 for c in chunks)
    assert_exact_coverage([section], chunks)


def test_practical_line_boundary_preferred(tokenizer, section):
    section = section.model_copy(update={"text": "a" * 299 + "\n" + "b" * 300})
    result = run([section], tokenizer)
    assert result.chunks[0].source.char_end == 300
    assert_exact_coverage([section], result.chunks)


def test_whitespace_offset_gaps_are_preserved(tokenizer, section):
    class Words(CharacterTokenizer):
        def count(self, text, *, add_special_tokens=False):
            return len(re.findall(r"\S+", text)) + (2 if add_special_tokens else 0)

        def offsets(self, text):
            return tuple((m.start(), m.end()) for m in re.finditer(r"\S+", text))

    section = section.model_copy(update={"text": " \t" + "word  " * 900 + "\n  "})
    chunks = run([section], Words()).chunks
    assert_exact_coverage([section], chunks)
    assert chunks[0].source.char_start == 0
    assert chunks[-1].source.char_end == len(section.text)


def test_shared_unicode_token_spans_progress_safely(tokenizer, section):
    class Shared(CharacterTokenizer):
        def count(self, text, *, add_special_tokens=False):
            return len(text) * 2 + (2 if add_special_tokens else 0)

        def offsets(self, text):
            return tuple(span for i in range(len(text)) for span in ((i, i + 1), (i, i + 1)))

    section = section.model_copy(update={"text": "క" * 500})
    result = run([section], Shared())
    assert_exact_coverage([section], result.chunks)


@pytest.mark.parametrize(
    "offsets",
    [
        [(-1, 2)],
        [(0, 0)],
        [(0, 1000)],
        [(1, 3), (0, 2)],
        [(0, 2), (1, 1)],
        [(True, 2)],
        [1],
        [(0, 1, 2)],
        "offsets",
    ],
)
def test_invalid_tokenizer_offsets_fail_safely(tokenizer, section, offsets):
    tokenizer.offsets = lambda text: offsets
    with pytest.raises(ChunkingError, match="invalid_token_offsets|section_token_limit"):
        run([section], tokenizer)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_chunks": 1},
        {"max_tokenizer_calls": 1},
        {"max_section_tokens": 20},
    ],
)
def test_resource_limit_rejects_without_partial_result(tokenizer, section, changes):
    section = section.model_copy(update={"text": "x" * 1000})
    with pytest.raises(ChunkingError, match="limit"):
        run([section], tokenizer, limits=replace(ChunkingLimits(), **changes))


def test_document_character_limit_checked_before_tokenization(tokenizer, section):
    tokenizer.offsets = lambda text: pytest.fail("tokenizer must not be called")
    with pytest.raises(ChunkingError, match="text_limit"):
        run([section], tokenizer, limits=ChunkingLimits(max_document_characters=2))


def test_mixed_document_or_unordered_sections_fail(tokenizer, section):
    with pytest.raises(ChunkingError, match="mixed_documents"):
        run(
            [
                section,
                section.model_copy(
                    update={"ordinal": 1, "section_id": uuid4(), "document_id": uuid4()}
                ),
            ],
            tokenizer,
        )
    with pytest.raises(ChunkingError, match="section_order"):
        run([section.model_copy(update={"ordinal": 1})], tokenizer)


def test_duplicate_section_ids_rejected(tokenizer, section):
    with pytest.raises(ChunkingError, match="duplicate_section"):
        run([section, section.model_copy(update={"ordinal": 1})], tokenizer)


def test_profile_revision_mismatch_rejected(tokenizer, section):
    other = tokenizer.profile.model_copy(update={"tokenizer_revision": "2" * 40})
    with pytest.raises(ChunkingError, match="tokenizer_profile_mismatch"):
        run([section], tokenizer, profile=other)


def test_unknown_chunker_is_rejected(tokenizer, section):
    tokenizer.profile = tokenizer.profile.model_copy(update={"chunker_version": "future_v2"})
    with pytest.raises(ChunkingError, match="unsupported_chunker_version"):
        run([section], tokenizer)


def test_nonempty_unencodable_text_rejected(tokenizer, section):
    tokenizer.offsets = lambda text: ()
    with pytest.raises(ChunkingError, match="section_unencodable"):
        run([section], tokenizer)


def test_unpaired_surrogate_rejected_without_private_text(tokenizer, section):
    private = "private\ud800content"
    with pytest.raises(ChunkingError) as exc:
        run([section.model_copy(update={"text": private})], tokenizer)
    assert str(exc.value) == "invalid_unicode"


def test_empty_document_is_valid(tokenizer):
    assert run([], tokenizer).chunks == ()


@pytest.mark.parametrize(
    "field",
    [
        "max_sections",
        "max_document_characters",
        "max_section_tokens",
        "max_chunks",
        "max_chunk_characters",
        "max_tokenizer_calls",
    ],
)
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_invalid_limits_rejected(field, invalid):
    with pytest.raises(ValueError):
        ChunkingLimits(**{field: invalid})


@pytest.mark.parametrize("invalid", [0, -1, True, float("nan"), float("inf"), 121])
def test_invalid_timeout_rejected(invalid):
    with pytest.raises(ValueError):
        ChunkingLimits(timeout_seconds=invalid)


@pytest.mark.parametrize(
    "change",
    [
        {"page_number": 0},
        {"line_start": 2},
        {"line_start": 5, "line_end": 3},
        {"heading": " "},
        {"ordinal": -1},
    ],
)
def test_invalid_source_references_rejected(section, change):
    values = {**section.model_dump(), **change}
    with pytest.raises(ValidationError):
        ExtractedSection(**values)


def test_content_is_omitted_from_representations(tokenizer, section):
    section = section.model_copy(update={"text": "private-document-text"})
    result = run([section], tokenizer)
    assert section.text not in repr(section)
    assert section.text not in repr(result)


@pytest.mark.parametrize("operation", ["count", "offsets"])
def test_tokenizer_exceptions_do_not_leak_source_content(tokenizer, section, operation):
    def fail(*args, **kwargs):
        raise RuntimeError("private source text and model filesystem path")

    setattr(tokenizer, operation, fail)
    with pytest.raises(ChunkingError) as exc:
        run([section], tokenizer)
    assert str(exc.value) == "tokenization_failed"
    assert exc.value.__suppress_context__


def test_slow_tokenizer_fails_cooperatively_without_partial_result(tokenizer, section, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("agentsphere.chunking.monotonic", lambda: clock[0])

    def slow(text):
        clock[0] = 61.0
        return ((0, len(text)),)

    tokenizer.offsets = slow
    with pytest.raises(ChunkingError, match="chunking_timeout"):
        run([section], tokenizer)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_sections", 10_001),
        ("max_document_characters", 2_000_001),
        ("max_section_tokens", 2_000_001),
        ("max_chunks", 10_001),
        ("max_chunk_characters", 16_001),
        ("max_tokenizer_calls", 100_001),
    ],
)
def test_absolute_resource_ceilings_cannot_be_disabled(field, value):
    with pytest.raises(ValueError):
        ChunkingLimits(**{field: value})


@pytest.mark.parametrize("invalid", [-1, True, 1.5, "one"])
def test_invalid_token_counts_rejected(tokenizer, section, invalid):
    tokenizer.count = lambda *args, **kwargs: invalid
    with pytest.raises(ChunkingError, match="invalid_token_count"):
        run([section], tokenizer)


def test_impossible_prefix_budget_fails_instead_of_truncating(tokenizer, section):
    tokenizer.count = lambda text, **kwargs: 513 if kwargs.get("add_special_tokens") else len(text)
    with pytest.raises(ChunkingError, match="section_unencodable"):
        run([section], tokenizer)


def test_different_files_or_parser_versions_change_source_identity(section):
    baseline = source_fingerprint([section], file_sha256="f" * 64, parser_version="v1")
    assert baseline != source_fingerprint([section], file_sha256="e" * 64, parser_version="v1")
    assert baseline != source_fingerprint([section], file_sha256="f" * 64, parser_version="v2")


@pytest.mark.parametrize(
    "file_hash,parser", [("bad", "v1"), ("F" * 64, "v1"), ("f" * 64, ""), ("f" * 64, "p" * 65)]
)
def test_invalid_source_digest_metadata(section, file_hash, parser):
    with pytest.raises(ChunkingError):
        source_fingerprint([section], file_sha256=file_hash, parser_version=parser)
