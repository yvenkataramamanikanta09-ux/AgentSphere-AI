"""Real PostgreSQL, isolated schemas, real cookie/CSRF authentication."""

from contextlib import ExitStack
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from agentsphere.config import BACKEND_DIR, Settings
from agentsphere.database import build_engine
from agentsphere.main import create_app

PASSWORD = "Test-only long passphrase 2026!"


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
    yield engine
    engine.dispose()


@pytest.fixture
def db_env(pg_engine):
    # Dedicated schemas support real commits and independent rate-limit connections.
    schema = "test_" + uuid4().hex
    with pg_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(
        pg_engine.url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": f"-c search_path={schema} -c timezone=UTC", "connect_timeout": 3},
    )
    try:
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        yield engine
    finally:
        engine.dispose()
        # Only the random schema created above, never public or an existing schema.
        with pg_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


@pytest.fixture
def clock():
    return [datetime(2026, 10, 16, 12, 0, tzinfo=UTC)]


@pytest.fixture
def client_factory(db_env, clock):
    with ExitStack() as stack:

        def make(**overrides):
            values = dict(
                _env_file=None,
                database_url=None,
                test_database_url=None,
                app_origin="https://localhost:8443",
                allow_insecure_local_http=False,
                auth_rate_limit_key="test-only-rate-key-not-for-production-1234",
                registration_ip_limit=100,
                csrf_ip_limit=1000,
                login_ip_limit=1000,
                login_account_limit=100,
                max_upload_bytes=10_000_000,
            )
            values.update(overrides)
            app = create_app(Settings(**values))
            app.state.clock = lambda: clock[0]
            client = stack.enter_context(TestClient(app, base_url="https://localhost:8443"))
            app.state.session_factory = sessionmaker(db_env, expire_on_commit=False)
            return client

        yield make


def bootstrap(client):
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response


def register_and_login(client, email=None):
    email = email or f"{uuid4().hex}@example.com"
    bootstrap(client)
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Test user"},
    )
    assert response.status_code == 201, response.text
    client.user_id = response.json()["user"]["id"]
    client.workspace_id = response.json()["workspace"]["id"]
    client.documents_url = f"/api/v1/workspaces/{client.workspace_id}/documents"
    logged_in = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert logged_in.status_code == 200, logged_in.text
    client.headers["X-CSRF-Token"] = logged_in.json()["csrf_token"]
    client.login_email = email
    return client


@pytest.fixture
def db_client(client_factory):
    return register_and_login(client_factory())
