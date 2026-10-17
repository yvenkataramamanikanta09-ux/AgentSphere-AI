import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agentsphere.config import Settings
from agentsphere.main import create_app


@pytest.mark.parametrize(
    "url",
    ["invalid", "sqlite:///example.db", "postgresql://localhost/app", "postgresql+psycopg:///app"],
)
def test_database_url_requires_postgresql_psycopg(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=url, test_database_url=None)


def test_database_url_is_redacted():
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://demo:example-password@localhost/demo",
        test_database_url=None,
    )
    assert "example-password" not in repr(settings)
    assert "example-password" not in settings.model_dump_json()


def test_no_database_preserves_module_one(client, metadata):
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.post("/api/v1/documents/validate", json=metadata).status_code == 200
    response = client.get("https://localhost:8443/api/v1/auth/me")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database is not configured."}


def test_unreachable_database_is_sanitized(metadata):
    settings = Settings(
        _env_file=None,
        max_upload_bytes=10_000_000,
        database_url="postgresql+psycopg://demo:example-password@127.0.0.1:1/unavailable",
        test_database_url=None,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.post("/api/v1/documents/validate", json=metadata).status_code == 200
        response = client.get(
            "https://localhost:8443/api/v1/auth/me",
            headers={"Cookie": "__Host-agentsphere_session=" + "a" * 43},
        )
        assert response.status_code == 503
        assert "example-password" not in response.text
        assert "SELECT" not in response.text
