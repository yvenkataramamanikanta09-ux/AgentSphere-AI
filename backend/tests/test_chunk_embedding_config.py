"""Stage 2 resource settings map into pure local chunking/inference limits."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentsphere.config import Settings
from agentsphere.search_config import SearchSettings


def test_stage2_resource_defaults_remain_local_inactive_and_bounded():
    settings = SearchSettings(_env_file=None)
    chunking = settings.search_chunking_limits
    embeddings = settings.search_embedding_limits
    assert chunking.max_sections == 10_000
    assert chunking.max_document_characters == 2_000_000
    assert chunking.max_chunks == 10_000
    assert chunking.max_tokenizer_calls == 100_000
    assert chunking.timeout_seconds == 60
    assert embeddings.batch_size == 32
    assert embeddings.cpu_threads == 2
    assert embeddings.max_total_characters == 128_000
    assert embeddings.max_query_tokens == 256
    assert embeddings.max_query_characters == 2_000
    assert not settings.search_enabled
    assert not settings.search_indexing_enabled
    assert not settings.search_hosted_embeddings_enabled
    assert settings.search_qdrant_url is None


def test_stage2_settings_properties_map_overrides_without_io(monkeypatch, tmp_path):
    monkeypatch.setattr(
        Path, "mkdir", lambda *args, **kwargs: pytest.fail("Unexpected path creation")
    )
    settings = Settings(
        _env_file=None,
        database_url=None,
        private_storage_root=tmp_path / "uncreated-private",
        search_model_cache=tmp_path / "uncreated-models",
        search_chunk_max_sections=40,
        search_chunk_max_characters=40_000,
        search_max_chunks_per_document=120,
        search_chunk_max_tokenizer_calls=800,
        search_chunk_timeout_seconds=12.5,
        search_batch_size=4,
        search_embedding_cpu_threads=1,
        search_embedding_max_batch_characters=800,
        search_query_max_tokens=64,
        search_query_max_characters=400,
    )
    assert settings.search_chunking_limits.max_sections == 40
    assert settings.search_chunking_limits.max_document_characters == 40_000
    assert settings.search_chunking_limits.max_chunks == 120
    assert settings.search_chunking_limits.max_tokenizer_calls == 800
    assert settings.search_chunking_limits.timeout_seconds == 12.5
    assert settings.search_embedding_limits.batch_size == 4
    assert settings.search_embedding_limits.cpu_threads == 1
    assert settings.search_embedding_limits.max_total_characters == 800
    assert settings.search_embedding_limits.max_query_tokens == 64
    assert settings.search_embedding_limits.max_query_characters == 400
    assert not list(tmp_path.iterdir())


def test_stage2_flat_environment_overrides_mapping_without_enabling_services(monkeypatch):
    monkeypatch.setenv("SEARCH_CHUNK_MAX_SECTIONS", "20")
    monkeypatch.setenv("SEARCH_CHUNK_MAX_CHARACTERS", "40000")
    monkeypatch.setenv("SEARCH_CHUNK_MAX_TOKENIZER_CALLS", "300")
    monkeypatch.setenv("SEARCH_CHUNK_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("SEARCH_EMBEDDING_CPU_THREADS", "3")
    monkeypatch.setenv("SEARCH_EMBEDDING_MAX_BATCH_CHARACTERS", "1000")
    settings = Settings(_env_file=None, database_url=None)
    assert settings.search_chunking_limits.max_sections == 20
    assert settings.search_chunking_limits.max_document_characters == 40_000
    assert settings.search_chunking_limits.max_tokenizer_calls == 300
    assert settings.search_chunking_limits.timeout_seconds == 3.5
    assert settings.search_embedding_limits.cpu_threads == 3
    assert settings.search_embedding_limits.max_total_characters == 1_000
    assert not settings.search_enabled
    assert not settings.search_indexing_enabled


@pytest.mark.parametrize(
    "field,value",
    [
        ("search_chunk_max_sections", 0),
        ("search_chunk_max_sections", 10_001),
        ("search_chunk_max_characters", 0),
        ("search_chunk_max_characters", 2_000_001),
        ("search_chunk_max_tokenizer_calls", 0),
        ("search_chunk_max_tokenizer_calls", 100_001),
        ("search_chunk_timeout_seconds", 0),
        ("search_chunk_timeout_seconds", -1),
        ("search_chunk_timeout_seconds", 120.1),
        ("search_chunk_timeout_seconds", float("nan")),
        ("search_chunk_timeout_seconds", float("inf")),
        ("search_embedding_cpu_threads", 0),
        ("search_embedding_cpu_threads", 9),
        ("search_embedding_max_batch_characters", 0),
        ("search_embedding_max_batch_characters", 2_048_001),
    ],
)
def test_invalid_stage2_resource_configuration_is_rejected(field, value):
    with pytest.raises(ValidationError):
        SearchSettings(_env_file=None, **{field: value})


@pytest.mark.parametrize(
    "field,value,property_name,attribute",
    [
        ("search_chunk_max_sections", 1, "search_chunking_limits", "max_sections"),
        ("search_chunk_max_sections", 10_000, "search_chunking_limits", "max_sections"),
        ("search_chunk_max_characters", 1, "search_chunking_limits", "max_document_characters"),
        (
            "search_chunk_max_characters",
            2_000_000,
            "search_chunking_limits",
            "max_document_characters",
        ),
        ("search_chunk_max_tokenizer_calls", 1, "search_chunking_limits", "max_tokenizer_calls"),
        (
            "search_chunk_max_tokenizer_calls",
            100_000,
            "search_chunking_limits",
            "max_tokenizer_calls",
        ),
        ("search_chunk_timeout_seconds", 0.1, "search_chunking_limits", "timeout_seconds"),
        ("search_chunk_timeout_seconds", 120, "search_chunking_limits", "timeout_seconds"),
        ("search_embedding_cpu_threads", 1, "search_embedding_limits", "cpu_threads"),
        ("search_embedding_cpu_threads", 8, "search_embedding_limits", "cpu_threads"),
        (
            "search_embedding_max_batch_characters",
            1,
            "search_embedding_limits",
            "max_total_characters",
        ),
        (
            "search_embedding_max_batch_characters",
            2_048_000,
            "search_embedding_limits",
            "max_total_characters",
        ),
    ],
)
def test_stage2_resource_limits_accept_their_documented_boundaries(
    field, value, property_name, attribute
):
    settings = SearchSettings(_env_file=None, **{field: value})
    assert getattr(getattr(settings, property_name), attribute) == value
