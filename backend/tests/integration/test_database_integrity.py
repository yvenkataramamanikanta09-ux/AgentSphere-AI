from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema

from agentsphere.config import BACKEND_DIR
from agentsphere.models import Document

pytestmark = pytest.mark.postgres


def migration_config(connection):
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_migrations_match_models(db_env):
    with db_env.begin() as connection:
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
def test_database_constraints_reject_invalid_direct_sql(db_env, db_client, changes):
    values = {
        "id": uuid4(),
        "workspace": db_client.workspace_id,
        "filename": "direct.txt",
        "content_type": "text/plain",
        "size": 1,
        "status": "registered",
    } | changes
    with db_env.connect() as connection:
        transaction = connection.begin()
        try:
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "INSERT INTO documents "
                            "(id,workspace_id,filename,content_type,size_bytes,status) "
                            "VALUES (:id,:workspace,:filename,:content_type,:size,:status)"
                        ),
                        values,
                    )
        finally:
            transaction.rollback()


def test_write_failure_rolls_back_and_hides_driver_details(db_env, db_client, metadata):
    generated = []

    def fail_after_insert(mapper, connection, target):
        generated.append(target.id)
        raise IntegrityError("simulated SQL", {}, RuntimeError("sensitive driver detail"))

    event.listen(Document, "after_insert", fail_after_insert)
    try:
        response = db_client.post(db_client.documents_url, json=metadata)
        assert response.status_code == 503
        assert "sensitive driver detail" not in response.text
        assert "simulated SQL" not in response.text
    finally:
        event.remove(Document, "after_insert", fail_after_insert)
    assert len(generated) == 1
    with Session(db_env) as session:
        assert session.get(Document, generated[0]) is None


def test_auth_migration_refuses_legacy_records_without_data_loss(pg_engine):
    from sqlalchemy.exc import DBAPIError

    schema = "migration_" + uuid4().hex
    with pg_engine.connect() as connection:
        outer = connection.begin()
        try:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            config = migration_config(connection)
            command.upgrade(config, "0001")
            connection.execute(
                text(
                    "INSERT INTO documents (id,filename,content_type,size_bytes) "
                    "VALUES (:id,'legacy.txt','text/plain',1)"
                ),
                {"id": uuid4()},
            )
            with pytest.raises(DBAPIError, match="explicit ownership mapping"):
                with connection.begin_nested():
                    command.upgrade(config, "head")
            assert connection.scalar(text("SELECT count(*) FROM documents")) == 1
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001"
            assert "users" not in inspect(connection).get_table_names(schema=schema)
        finally:
            outer.rollback()
