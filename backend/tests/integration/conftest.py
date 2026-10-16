"""Real PostgreSQL tests. Each HTTP test rolls back its outer transaction."""

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from agentsphere.config import BACKEND_DIR, Settings
from agentsphere.database import build_engine, get_session
from agentsphere.main import create_app


def migration_config(connection):
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="session")
def pg_engine():
    settings = Settings()
    if settings.test_database_url is None:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests.")
    url = make_url(settings.test_database_url.get_secret_value())
    if not url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must name a dedicated database ending in _test.")
    if settings.database_url is not None:
        app_url = make_url(settings.database_url.get_secret_value())
        if (url.host, url.port, url.database) == (app_url.host, app_url.port, app_url.database):
            pytest.fail("Development and test databases must be different.")
    engine = build_engine(settings.test_database_url.get_secret_value())
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_client(pg_engine):
    settings = Settings(
        _env_file=None, max_upload_bytes=10_000_000, database_url=None, test_database_url=None
    )
    app = create_app(settings)
    with pg_engine.connect() as connection:
        outer = connection.begin()

        def isolated_session():
            with Session(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ) as session:
                yield session

        app.dependency_overrides[get_session] = isolated_session
        try:
            with TestClient(app) as client:
                yield client
        finally:
            outer.rollback()
