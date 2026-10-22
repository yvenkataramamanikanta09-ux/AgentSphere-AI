"""Fail-closed settings without creating paths, loading models or contacting Qdrant."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentsphere.config import Settings
from agentsphere.search_config import SearchSettings

PINS = {"search_model_revision": "a" * 40, "search_tokenizer_revision": "b" * 40}
KEYS = {"search_qdrant_read_key": "r" * 40, "search_qdrant_write_key": "w" * 40}


def test_search_is_inactive_without_credentials_connection_or_model_revision():
    settings = Settings(_env_file=None)
    assert not settings.search_enabled
    assert not settings.search_indexing_enabled
    assert not settings.search_hosted_embeddings_enabled
    assert settings.search_qdrant_url is None
    assert settings.search_qdrant_read_key is None
    assert settings.search_model_revision is None
    assert settings.search_chunk_tokens == 350
    assert settings.search_chunk_overlap_tokens == 50
    assert settings.search_embedding_dimensions == 384
    with pytest.raises(ValueError, match="requires pinned"):
        _ = settings.search_profile


def test_constructing_configuration_never_creates_model_cache_or_private_storage(tmp_path):
    cache, storage = tmp_path / "models", tmp_path / "originals"
    Settings(_env_file=None, search_model_cache=cache, private_storage_root=storage)
    assert not cache.exists()
    assert not storage.exists()
    assert not list(tmp_path.iterdir())


def test_flat_search_environment_and_dotenv_precedence_follow_existing_settings(
    monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SEARCH_CHUNK_TOKENS=300\nSEARCH_CHUNK_OVERLAP_TOKENS=25\n"
        "SEARCH_EMBEDDING_DIMENSIONS=384\nSEARCH_HOSTED_EMBEDDINGS_ENABLED=false\n"
        "SEARCH_OFFLINE_MODELS=true\nSEARCH_TRUST_REMOTE_CODE=false\n"
        "SEARCH_QDRANT_VERIFY_TLS=true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SEARCH_CHUNK_TOKENS", raising=False)
    assert Settings(_env_file=env_file).search_chunk_tokens == 300
    assert Settings(_env_file=None).search_chunk_tokens == 350
    monkeypatch.setenv("SEARCH_CHUNK_TOKENS", "400")
    assert Settings(_env_file=env_file).search_chunk_tokens == 400
    assert Settings(_env_file=env_file, search_chunk_tokens=450).search_chunk_tokens == 450


def test_enabling_valid_configuration_is_only_validation_and_returns_pinned_contract():
    settings = SearchSettings(
        _env_file=None,
        **PINS,
        **KEYS,
        search_enabled=True,
        search_indexing_enabled=True,
        search_qdrant_url="https://qdrant:6333",
    )
    assert settings.search_profile.model_revision == "a" * 40
    assert settings.search_profile.tokenizer_revision == "b" * 40
    assert settings.search_profile.query_prefix == "query: "
    assert "r" * 40 not in repr(settings)
    assert "w" * 40 not in settings.model_dump_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"search_enabled": True},
        {"search_indexing_enabled": True},
        {"search_enabled": True, **PINS},
        {"search_enabled": True, **PINS, "search_qdrant_url": "https://qdrant:6333"},
        {
            "search_indexing_enabled": True,
            **PINS,
            "search_qdrant_url": "https://qdrant:6333",
            "search_qdrant_read_key": "r" * 40,
        },
        {"search_hosted_embeddings_enabled": True},
        {"search_embedding_provider": "openai"},
        {"search_model_name": "unreviewed/model"},
        {"search_model_revision": "main"},
        {"search_model_revision": "A" * 40},
        {"search_tokenizer_revision": "a" * 39},
        {"search_embedding_dimensions": 1536},
        {"search_offline_models": False},
        {"search_trust_remote_code": True},
        {"search_qdrant_verify_tls": False},
        {"search_qdrant_read_key": "too-short"},
        {"search_qdrant_write_key": "x" * 32 + "\n"},
        {"search_qdrant_read_key": "x" * 40, "search_qdrant_write_key": "x" * 40},
        {"search_chunk_tokens": 481},
        {"search_chunk_tokens": 50, "search_chunk_overlap_tokens": 50},
        {"search_chunk_overlap_tokens": -1},
        {"search_collection_prefix": "../other"},
        {"search_batch_size": 129},
        {"search_max_chunks_per_document": 10_001},
        {"search_embedding_timeout_seconds": 121},
        {"search_qdrant_timeout_seconds": 31},
        {"search_worker_lease_seconds": 60},
        {"search_worker_max_attempts": 6},
        {"search_worker_retry_base_seconds": 31},
        {"search_worker_poll_seconds": 0},
        {"search_inference_concurrency": 5},
        {"search_query_max_tokens": 257},
        {"search_query_max_characters": 2001},
        {"search_query_max_body_bytes": 16_385},
        {"search_query_max_results": 51},
        {"search_query_requests_per_minute": 121},
        {"search_query_timeout_seconds": 0},
        {"search_reconciliation_min_age_seconds": 3599},
        {"search_reconciliation_max_entries": 0},
    ],
)
def test_unsafe_incomplete_or_unbounded_configuration_fails(changes):
    with pytest.raises(ValidationError):
        SearchSettings(_env_file=None, **changes)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6333",
        "http://qdrant:6333",
        "https://public.example:6333",
        "https://user:password@qdrant:6333",
        "https://qdrant:6333/collections",
        "https://qdrant:6333/",
        "https://qdrant:6333?api_key=secret",
        "https://qdrant:6333#fragment",
        "https://qdrant:0",
        "https://qdrant:65536",
        "https://qdrant:invalid",
        "https://[invalid",
        "file:///private/vector-index",
        ":memory:",
        "https://qdrant\n:6333",
    ],
)
def test_qdrant_url_requires_explicit_allowed_private_origin(url):
    with pytest.raises(ValidationError):
        SearchSettings(_env_file=None, search_qdrant_url=url)


def test_insecure_qdrant_requires_explicit_loopback_development_permission():
    settings = SearchSettings(
        _env_file=None,
        search_qdrant_url="http://127.0.0.1:6333",
        search_allow_insecure_local_qdrant=True,
    )
    assert settings.search_qdrant_url == "http://127.0.0.1:6333"
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            auth_rate_limit_key="r" * 40,
            search_allow_insecure_local_qdrant=True,
        )
    with pytest.raises(ValidationError):
        SearchSettings(
            _env_file=None,
            search_qdrant_url="http://qdrant:6333",
            search_allow_insecure_local_qdrant=True,
        )


@pytest.mark.parametrize("hosts", [(), ("*",), ("Qdrant",), ("qdrant/path",), ("qdrant", "qdrant")])
def test_qdrant_host_allowlist_requires_explicit_unique_hosts(hosts):
    with pytest.raises(ValidationError):
        SearchSettings(_env_file=None, search_qdrant_allowed_hosts=hosts)


@pytest.mark.parametrize("path", ["relative/models", "../models", "C:/", "C:/safe/../models"])
def test_model_cache_rejects_relative_root_and_parent_traversal(path):
    with pytest.raises(ValidationError):
        SearchSettings(_env_file=None, search_model_cache=path)


def test_model_cache_rejects_unc_and_device_paths_on_windows():
    if Path().anchor == "":
        # Strings below are invalid on either platform; no Windows filesystem required.
        for path in (r"\\server\private\models", r"\\?\C:\models"):
            with pytest.raises(ValidationError):
                SearchSettings(_env_file=None, search_model_cache=path)


def test_hostile_endpoint_credentials_are_not_printed_in_configuration_errors():
    with pytest.raises(ValidationError) as caught:
        SearchSettings(_env_file=None, search_qdrant_url="https://user:private-password@qdrant")
    assert "private-password" not in str(caught.value)


def test_example_template_is_valid_with_every_inactive_search_setting():
    example = Path(__file__).resolve().parents[1] / ".env.example"
    settings = Settings(_env_file=example)
    assert not settings.search_enabled
    assert settings.search_model_revision is None
    assert settings.search_qdrant_allowed_hosts[-1] == "qdrant"
