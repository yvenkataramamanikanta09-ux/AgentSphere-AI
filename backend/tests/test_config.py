import pytest
from pydantic import ValidationError

from agentsphere.config import Settings


def test_default_limit(monkeypatch):
    monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    assert Settings(_env_file=None).max_upload_bytes == 10_000_000


def test_environment_overrides_dotenv(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MAX_UPLOAD_BYTES=500\n", encoding="utf-8")
    monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    assert Settings(_env_file=env_file).max_upload_bytes == 500
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "750")
    assert Settings(_env_file=env_file).max_upload_bytes == 750


@pytest.mark.parametrize("value", ["0", "-1", "10000001", "invalid"])
def test_invalid_environment_configuration(monkeypatch, value):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("upload_timeout_seconds", 0),
        ("upload_timeout_seconds", 601),
        ("upload_concurrency", 0),
        ("upload_concurrency", 9),
    ],
)
def test_upload_resource_controls_are_bounded(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("extraction_timeout_seconds", 0),
        ("extraction_timeout_seconds", 61),
        ("extraction_memory_mb", 127),
        ("extraction_memory_mb", 2049),
        ("extraction_max_pdf_pages", 501),
        ("extraction_max_text_bytes", 2_000_001),
        ("extraction_max_sections", 5001),
        ("extraction_output_bytes", 32_000_001),
        ("docx_max_entries", 1025),
        ("docx_max_expanded_bytes", 40_000_001),
        ("docx_max_entry_bytes", 20_000_001),
        ("docx_max_expansion_ratio", 101),
        ("worker_poll_seconds", 0),
        ("worker_retry_base_seconds", 31),
        ("reconciliation_min_age_seconds", 3599),
        ("reconciliation_max_entries", 0),
    ],
)
def test_extraction_and_maintenance_limits_are_bounded(name, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


@pytest.mark.parametrize(
    "options",
    [
        {"extraction_timeout_seconds": 60, "worker_lease_seconds": 60},
        {"extraction_output_bytes": 65_536},
        {"docx_max_expanded_bytes": 1024, "docx_max_entry_bytes": 2048},
    ],
)
def test_inconsistent_extraction_limits_fail_closed(options):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **options)


def test_settings_produce_matching_parser_limits():
    settings = Settings(_env_file=None, extraction_max_pdf_pages=10)
    assert settings.extraction_limits.max_pdf_pages == 10
    assert settings.extraction_limits.timeout_seconds == 60


def test_output_budget_defaults_cover_unicode_references_and_json_escaping():
    settings = Settings(_env_file=None)
    assert settings.extraction_output_bytes == 20_000_000
    minimum = (
        2 * settings.extraction_max_text_bytes + settings.extraction_max_sections * 2560 + 1024
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, extraction_output_bytes=minimum - 1)
    assert (
        Settings(_env_file=None, extraction_output_bytes=minimum).extraction_output_bytes == minimum
    )
