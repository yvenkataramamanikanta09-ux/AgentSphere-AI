import pytest
from fastapi.testclient import TestClient

from agentsphere.config import Settings
from agentsphere.main import create_app


@pytest.fixture
def client():
    settings = Settings(_env_file=None, max_upload_bytes=10_000_000)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def metadata():
    return {"filename": "proposal.pdf", "content_type": "application/pdf", "size_bytes": 1024}
