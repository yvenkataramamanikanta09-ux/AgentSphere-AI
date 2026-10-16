from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema

from agentsphere.config import BACKEND_DIR, Settings
from agentsphere.main import create_app
from agentsphere.models import Document

pytestmark = pytest.mark.postgres


def migration_config(connection):
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_migrations_match_models(pg_engine):
    with pg_engine.begin() as connection:
        command.check(migration_config(connection))


def test_migration_upgrade_downgrade_in_isolated_schema(pg_engine):
    schema = "migration_" + uuid4().hex
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = migration_config(connection)
            command.upgrade(config, "head")
            assert "documents" in inspect(connection).get_table_names(schema=schema)
            command.downgrade(config, "base")
            assert "documents" not in inspect(connection).get_table_names(schema=schema)
            command.upgrade(config, "head")
            assert "documents" in inspect(connection).get_table_names(schema=schema)
        finally:
            transaction.rollback()


@pytest.mark.parametrize(
    "changes",
    [{"size": 0}, {"size": -1}, {"status": "uploaded"}, {"filename": ""}, {"content_type": ""}],
)
def test_database_constraints_reject_invalid_direct_sql(pg_engine, changes):
    values = {
        "id": uuid4(),
        "filename": "direct.txt",
        "content_type": "text/plain",
        "size": 1,
        "status": "registered",
    } | changes
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "INSERT INTO documents (id,filename,content_type,size_bytes,status) "
                            "VALUES (:id,:filename,:content_type,:size,:status)"
                        ),
                        values,
                    )
        finally:
            transaction.rollback()


def test_commit_failure_rolls_back_and_hides_driver_details(pg_engine, metadata):
    settings = Settings()
    app_settings = Settings(
        _env_file=None,
        database_url=settings.test_database_url,
        test_database_url=None,
        max_upload_bytes=10_000_000,
    )
    generated = []

    def fail_after_insert(mapper, connection, target):
        generated.append(target.id)
        raise IntegrityError("simulated SQL", {}, RuntimeError("sensitive driver detail"))

    event.listen(Document, "after_insert", fail_after_insert)
    try:
        with TestClient(create_app(app_settings)) as client:
            response = client.post("/api/v1/documents", json=metadata)
            assert response.status_code == 503
            assert "sensitive driver detail" not in response.text
            assert "simulated SQL" not in response.text
    finally:
        event.remove(Document, "after_insert", fail_after_insert)
    assert len(generated) == 1
    with Session(pg_engine) as session:
        assert session.get(Document, generated[0]) is None
