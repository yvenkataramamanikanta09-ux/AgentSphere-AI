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


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_invalid_environment_configuration(monkeypatch, value):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
