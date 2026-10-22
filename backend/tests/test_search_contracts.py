"""Pure search contracts: synthetic pins and text, no inference or service calls."""

import hashlib
import json
from itertools import product
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentsphere.search_contracts import (
    INDEX_TRANSITIONS,
    JOB_TRANSITIONS,
    EmbeddingProfileSpec,
    IndexState,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SourceReference,
    VectorJobState,
    stable_chunk_id,
    validate_index_transition,
    validate_vector_job_transition,
)

# Deliberately synthetic commit strings; no actual model pin/download is asserted.
PINS = {"model_revision": "a" * 40, "tokenizer_revision": "b" * 40}


def chunk_identity(**changes):
    return {
        "document_id": uuid4(),
        "source_fingerprint": "a" * 64,
        "profile_fingerprint": "b" * 64,
        "content_sha256": "c" * 64,
        "section_ordinal": 0,
        "char_start": 0,
        "char_end": 20,
    } | changes


def test_logical_chunk_identity_is_stable_without_a_transient_section_uuid():
    identity = chunk_identity()
    assert stable_chunk_id(**identity) == stable_chunk_id(**dict(reversed(list(identity.items()))))
    assert stable_chunk_id(**identity).version == 5


@pytest.mark.parametrize(
    "changes",
    [
        {"document_id": uuid4()},
        {"source_fingerprint": "d" * 64},
        {"profile_fingerprint": "d" * 64},
        {"content_sha256": "d" * 64},
        {"section_ordinal": 1},
        {"char_start": 1},
        {"char_end": 21},
    ],
)
def test_logical_chunk_identity_changes_with_document_source_profile_content_or_position(changes):
    identity = chunk_identity()
    assert stable_chunk_id(**identity) != stable_chunk_id(**(identity | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"document_id": "not-a-uuid"},
        {"source_fingerprint": "A" * 64},
        {"profile_fingerprint": "x" * 64},
        {"content_sha256": "a" * 63},
        {"section_ordinal": -1},
        {"section_ordinal": True},
        {"char_start": -1},
        {"char_start": "0"},
        {"char_end": 0},
        {"char_end": 0.5},
    ],
)
def test_invalid_logical_chunk_identity_inputs_fail(changes):
    with pytest.raises(ValueError):
        stable_chunk_id(**chunk_identity(**changes))


def test_profile_fingerprint_is_canonical_and_includes_explicit_defaults():
    profile = EmbeddingProfileSpec(**PINS)
    reverse_input = dict(reversed(list(profile.model_dump().items())))
    rebuilt = EmbeddingProfileSpec(**reverse_input)
    encoded = json.dumps(
        profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert rebuilt.fingerprint == profile.fingerprint == hashlib.sha256(encoded).hexdigest()
    assert len(profile.fingerprint) == 64
    assert "fingerprint" not in profile.model_dump()
    assert profile.dimensions == 384
    assert profile.chunk_tokens == 350
    assert profile.chunk_overlap_tokens == 50


@pytest.mark.parametrize(
    "changes",
    [
        {"model_revision": "c" * 40},
        {"tokenizer_revision": "c" * 40},
        {"chunk_tokens": 300},
        {"chunk_overlap_tokens": 0},
        {"chunker_version": "section_tokens_v2"},
    ],
)
def test_semantic_profile_changes_have_distinct_fingerprints(changes):
    original = EmbeddingProfileSpec(**PINS)
    changed = EmbeddingProfileSpec(**(PINS | changes))
    assert changed.fingerprint != original.fingerprint


def test_profile_configuration_is_frozen_and_cannot_accept_a_supplied_fingerprint():
    profile = EmbeddingProfileSpec(**PINS)
    with pytest.raises(ValidationError):
        profile.chunk_tokens = 200
    with pytest.raises(ValidationError):
        EmbeddingProfileSpec(**PINS, fingerprint="0" * 64)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "openai"},
        {"model_name": "unreviewed/remote-code"},
        {"tokenizer_name": "unreviewed/tokenizer"},
        {"model_revision": "main"},
        {"model_revision": "A" * 40},
        {"model_revision": "a" * 39},
        {"model_revision": "a" * 40 + "\n"},
        {"tokenizer_revision": "latest"},
        {"tokenizer_revision": "g" * 40},
        {"dimensions": 1536},
        {"model_max_tokens": 8192},
        {"chunk_tokens": 0},
        {"chunk_tokens": 481},
        {"chunk_tokens": "350"},
        {"chunk_tokens": True},
        {"chunk_tokens": 50, "chunk_overlap_tokens": 50},
        {"chunk_overlap_tokens": -1},
        {"normalize_embeddings": False},
        {"query_prefix": ""},
        {"passage_prefix": "document: "},
        {"distance_metric": "euclidean"},
        {"chunker_version": "../../unsafe"},
        {"profile_schema_version": 2},
    ],
)
def test_invalid_or_unapproved_profiles_fail_closed(changes):
    with pytest.raises(ValidationError):
        EmbeddingProfileSpec(**(PINS | changes))


@pytest.mark.parametrize("current,target", list(product(IndexState, repeat=2)))
def test_index_state_transition_matrix_prevents_rebuilding_published_generations(current, target):
    if target == current or target in INDEX_TRANSITIONS[current]:
        validate_index_transition(current, target)
    else:
        with pytest.raises(ValueError, match="Invalid indexing-generation"):
            validate_index_transition(current, target)


@pytest.mark.parametrize("current,target", list(product(VectorJobState, repeat=2)))
def test_job_state_transition_matrix_requires_new_jobs_after_terminal_outcomes(current, target):
    if target == current or target in JOB_TRANSITIONS[current]:
        validate_vector_job_transition(current, target)
    else:
        with pytest.raises(ValueError, match="Invalid vector-job"):
            validate_vector_job_transition(current, target)


def test_unknown_states_are_rejected():
    with pytest.raises(ValueError):
        validate_index_transition("building", "published_anyway")
    with pytest.raises(ValueError):
        validate_vector_job_transition("running", "ready")


def test_search_request_keeps_unicode_and_normalizes_only_surrounding_whitespace():
    document = uuid4()
    request = SearchRequest(query="  తెలుగు backups?\n  ", document_ids=[str(document)])
    assert request.query == "తెలుగు backups?"
    assert request.document_ids == (document,)
    assert request.limit == 10
    assert request.include_archived is False


@pytest.mark.parametrize(
    "values",
    [
        {"query": ""},
        {"query": "\n \t"},
        {"query": "x" * 2001},
        {"query": "find\x00private"},
        {"query": "find\x1bprivate"},
        {"query": "find\ud800private"},
        {"limit": 0},
        {"limit": 51},
        {"limit": "10"},
        {"limit": True},
        {"document_ids": []},
        {"document_ids": [uuid4() for _ in range(51)]},
        {"document_ids": ["../other-workspace"]},
        {"collection": "arbitrary_collection"},
        {"workspace_id": str(uuid4())},
        {"filter": {"workspace_id": "other"}},
        {"endpoint": "http://untrusted"},
    ],
)
def test_search_input_rejects_unbounded_or_vector_service_control_fields(values):
    with pytest.raises(ValidationError):
        SearchRequest(**({"query": "backups"} | values))


def test_duplicate_document_filters_are_rejected():
    document = uuid4()
    with pytest.raises(ValidationError):
        SearchRequest(query="backups", document_ids=[document, document])


def test_request_char_limit_is_not_claimed_to_be_a_tokenizer_check():
    # Actual model-token validation deliberately belongs to Stage 2/4.
    assert SearchRequest(query="測試" * 1000, limit=50).limit == 50


def reference(**changes):
    return SourceReference(
        **({"section_id": uuid4(), "section_ordinal": 0, "char_start": 2, "char_end": 8} | changes)
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"section_ordinal": -1},
        {"char_start": -1},
        {"char_end": 2},
        {"page_number": 0},
        {"line_start": 1},
        {"line_end": 2},
        {"line_start": 5, "line_end": 4},
        {"heading": "   "},
        {"offset_unit": "file_bytes"},
        {"line_precision": "exact_chunk"},
    ],
)
def test_invalid_source_references_fail(changes):
    with pytest.raises(ValidationError):
        reference(**changes)


def test_references_preserve_parent_line_range_without_inventing_locations():
    plain = reference(line_start=10, line_end=89)
    assert plain.line_precision == "section"
    assert plain.page_number is None
    assert plain.heading is None
    assert plain.offset_unit == "unicode_codepoints"
    docx = reference(heading="Table 2, row 3 · Budget")
    assert docx.page_number is None


def result(**changes):
    return SearchResult(
        **(
            {
                "document_id": uuid4(),
                "filename": "legacy:metadata.txt",
                "chunk_id": uuid4(),
                "index_version_id": uuid4(),
                "text": "A verified passage.",
                "score": 0.8,
                "source": reference(),
            }
            | changes
        )
    )


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -1.01, 1.01])
def test_similarity_scores_must_be_finite_cosine_values(score):
    with pytest.raises(ValidationError):
        result(score=score)


def test_response_has_no_fabricated_total_confidence_or_unverified_service_payload():
    response = SearchResponse(
        profile_id=uuid4(),
        profile_fingerprint="a" * 64,
        requested_limit=10,
        results=[result()],
    )
    body = response.model_dump()
    assert set(body) == {"profile_id", "profile_fingerprint", "requested_limit", "results"}
    assert response.results[0].score_kind == "cosine_similarity"
    assert "confidence" not in body["results"][0]


def test_response_rejects_duplicate_chunks_and_excess_results():
    hit = result()
    values = {"profile_id": uuid4(), "profile_fingerprint": "a" * 64, "requested_limit": 1}
    with pytest.raises(ValidationError):
        SearchResponse(**values, results=[hit, result()])
    with pytest.raises(ValidationError):
        SearchResponse(**(values | {"requested_limit": 2}), results=[hit, hit])
