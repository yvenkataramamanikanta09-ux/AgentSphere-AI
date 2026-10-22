"""Run real migrations and constraints only inside disposable PostgreSQL schemas."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema

from agentsphere.config import BACKEND_DIR
from agentsphere.models import (
    Document,
    DocumentFile,
    DocumentJob,
    DocumentSection,
    User,
    Workspace,
)

pytestmark = pytest.mark.postgres

LEGACY_TABLES = (
    "documents",
    "users",
    "sessions",
    "workspaces",
    "workspace_memberships",
    "auth_rate_limits",
)
NEW_TABLES = {"document_files", "document_sections", "document_jobs"}
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def migration_config(connection):
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture
def migration_connection(pg_engine):
    schema = "migration_" + uuid4().hex
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            yield connection
        finally:
            # The schema and everything in it were created in this transaction.
            transaction.rollback()


def legacy_snapshot(connection):
    # Serialize every column, including timestamps and auth values, without logging them.
    return {
        table: tuple(
            connection.scalars(
                text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text")
            )
        )
        for table in LEGACY_TABLES
    }


def seed_module4(connection):
    owner, member, workspace, registered, archived = [uuid4() for _ in range(5)]
    connection.execute(
        text(
            "INSERT INTO users (id,email,password_hash,display_name) VALUES "
            "(:owner,'owner@example.invalid','unused-test-hash','Owner'),"
            "(:member,'member@example.invalid','unused-test-hash','Member')"
        ),
        {"owner": owner, "member": member},
    )
    connection.execute(
        text("INSERT INTO workspaces (id,name,owner_user_id) VALUES (:id,'Legacy',:owner)"),
        {"id": workspace, "owner": owner},
    )
    connection.execute(
        text(
            "INSERT INTO workspace_memberships (workspace_id,user_id,role) "
            "VALUES (:workspace,:member,'editor')"
        ),
        {"workspace": workspace, "member": member},
    )
    connection.execute(
        text(
            "INSERT INTO documents (id,workspace_id,filename,content_type,size_bytes,status) "
            "VALUES (:registered,:workspace,'résumé.txt','text/plain',4,'registered'),"
            "(:archived,:workspace,'archive.md','text/markdown',8,'archived')"
        ),
        {"registered": registered, "archived": archived, "workspace": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO sessions "
            "(id,token_digest,user_id,csrf_token,created_at,last_seen_at,expires_at) "
            "VALUES (:id,:digest,:owner,:csrf,:now,:now,:expiry)"
        ),
        {
            "id": uuid4(),
            "digest": "1" * 64,
            "csrf": "2" * 64,
            "owner": owner,
            "now": NOW,
            "expiry": NOW + timedelta(hours=1),
        },
    )
    connection.execute(
        text(
            "INSERT INTO auth_rate_limits "
            "(scope,identity_digest,window_start,count,expires_at) "
            "VALUES ('login_ip',:digest,1,2,:expiry)"
        ),
        {"digest": "3" * 64, "expiry": NOW + timedelta(minutes=1)},
    )
    return owner, workspace, registered


def test_clean_database_upgrade_has_empty_storage_tables_and_matches_models(migration_connection):
    connection = migration_connection
    command.upgrade(migration_config(connection), "head")
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
    assert NEW_TABLES <= set(inspect(connection).get_table_names())
    for table in NEW_TABLES:
        assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    command.check(migration_config(connection))


def test_populated_module4_upgrade_and_empty_downgrade_preserve_every_legacy_value(
    migration_connection,
):
    connection = migration_connection
    config = migration_config(connection)
    command.upgrade(config, "0002")
    seed_module4(connection)
    before = legacy_snapshot(connection)
    document_columns = [
        (column["name"], str(column["type"]), column["nullable"], column["default"])
        for column in inspect(connection).get_columns("documents")
    ]
    command.upgrade(config, "0003")
    assert legacy_snapshot(connection) == before
    assert [
        (column["name"], str(column["type"]), column["nullable"], column["default"])
        for column in inspect(connection).get_columns("documents")
    ] == document_columns
    assert connection.scalar(text("SELECT count(*) FROM document_files")) == 0
    command.downgrade(config, "0002")
    assert legacy_snapshot(connection) == before
    assert NEW_TABLES.isdisjoint(inspect(connection).get_table_names())
    command.upgrade(config, "head")
    assert legacy_snapshot(connection) == before
    command.check(config)


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
def test_downgrade_refuses_any_new_data_without_losing_legacy_records(migration_connection, table):
    connection = migration_connection
    config = migration_config(connection)
    command.upgrade(config, "0002")
    owner, workspace, document = seed_module4(connection)
    command.upgrade(config, "0003")
    storage_key = f"objects/{workspace.hex}/{uuid4().hex}/original"
    rows = {
        "document_files": (
            DocumentFile.__table__,
            dict(
                document_id=document,
                storage_key=storage_key,
                uploaded_by=owner,
                original_filename="original.txt",
                sha256="a" * 64,
            ),
        ),
        "document_sections": (
            DocumentSection.__table__,
            dict(document_id=document, ordinal=0, text="Legacy text"),
        ),
        "document_jobs": (
            DocumentJob.__table__,
            dict(document_id=document, storage_key=storage_key, kind="extract"),
        ),
    }
    model_table, values = rows[table]
    connection.execute(model_table.insert().values(**values))
    before = legacy_snapshot(connection)
    with pytest.raises(DBAPIError, match="Downgrade refused: document storage"):
        with connection.begin_nested():
            command.downgrade(config, "0002")
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
    assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
    assert legacy_snapshot(connection) == before


@pytest.fixture
def storage_records(db_env):
    with Session(db_env) as session, session.begin():
        user = User(
            email=f"{uuid4().hex}@example.invalid", password_hash="unused", display_name="Test"
        )
        session.add(user)
        session.flush()
        workspace = Workspace(name="Storage tests", owner_user_id=user.id)
        session.add(workspace)
        session.flush()
        documents = [
            Document(
                workspace_id=workspace.id,
                filename=f"{index}.txt",
                content_type="text/plain",
                size_bytes=5,
            )
            for index in range(2)
        ]
        session.add_all(documents)
        session.flush()
        return {
            "user": user.id,
            "document": documents[0].id,
            "other_document": documents[1].id,
            "storage_key": f"objects/{workspace.id.hex}/{uuid4().hex}/original",
        }


def file_values(records):
    return dict(
        document_id=records["document"],
        uploaded_by=records["user"],
        storage_key=records["storage_key"],
        original_filename="original.txt",
        sha256="a" * 64,
    )


def job_values(records):
    return dict(document_id=records["document"], storage_key=records["storage_key"], kind="extract")


def reject_values(db_env, model, values):
    with db_env.begin() as connection:
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(model.__table__.insert().values(**values))


@pytest.mark.parametrize(
    "changes",
    [
        {"document_id": uuid4()},
        {"uploaded_by": uuid4()},
        {"sha256": "invalid"},
        {"storage_key": "../outside"},
        {"storage_key": "objects/" + "A" * 32 + "/" + "a" * 32 + "/original"},
        {"original_filename": " "},
        {"processing_status": "registered"},
        {"error_code": "contains private details"},
        {"warning_code": "../path"},
        {"parser_version": " "},
    ],
)
def test_file_constraints_reject_invalid_rows(db_env, storage_records, changes):
    reject_values(db_env, DocumentFile, file_values(storage_records) | changes)


def test_one_file_per_document_and_unique_storage_key(db_env, storage_records):
    values = file_values(storage_records)
    with db_env.begin() as connection:
        connection.execute(DocumentFile.__table__.insert().values(**values))
    reject_values(
        db_env,
        DocumentFile,
        values | {"storage_key": f"objects/{uuid4().hex}/{uuid4().hex}/original"},
    )
    reject_values(db_env, DocumentFile, values | {"document_id": storage_records["other_document"]})


@pytest.mark.parametrize(
    "changes",
    [
        {"document_id": uuid4()},
        {"ordinal": -1},
        {"text": ""},
        {"page_number": 0},
        {"line_start": 0, "line_end": 2},
        {"line_start": 3, "line_end": 2},
        {"line_start": 1},
        {"line_end": 2},
        {"heading": " "},
    ],
)
def test_section_constraints_reject_invalid_rows(db_env, storage_records, changes):
    values = (
        dict(document_id=storage_records["document"], ordinal=0, text="Extracted text") | changes
    )
    reject_values(db_env, DocumentSection, values)


def test_sections_are_unique_and_ordered_per_document(db_env, storage_records):
    with db_env.begin() as connection:
        for ordinal in (1, 0):
            connection.execute(
                DocumentSection.__table__.insert().values(
                    document_id=storage_records["document"],
                    ordinal=ordinal,
                    text=f"Section {ordinal}",
                )
            )
        assert connection.scalars(
            select(DocumentSection.ordinal).order_by(DocumentSection.ordinal)
        ).all() == [0, 1]
    reject_values(
        db_env,
        DocumentSection,
        dict(document_id=storage_records["document"], ordinal=0, text="Duplicate"),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"document_id": uuid4()},
        {"storage_key": "../outside"},
        {"kind": "unknown"},
        {"state": "invalid"},
        {"attempts": -1},
        {"max_attempts": 0},
        {"attempts": 4, "max_attempts": 3},
        {"state": "running"},
        {"lease_token": uuid4()},
        {"lease_expires_at": NOW},
        {"state": "succeeded"},
        {"finished_at": NOW},
        {"error_code": "Sensitive error details"},
        {
            "state": "running",
            "attempts": 1,
            "started_at": NOW,
            "lease_token": uuid4(),
            "lease_expires_at": NOW,
        },
    ],
)
def test_job_constraints_reject_invalid_rows(db_env, storage_records, changes):
    reject_values(db_env, DocumentJob, job_values(storage_records) | changes)


def test_active_job_uniqueness_and_valid_lease_state_changes(db_env, storage_records):
    values = job_values(storage_records)
    with Session(db_env) as session, session.begin():
        job = DocumentJob(**values)
        session.add(job)
        session.flush()
        job_id = job.id
    reject_values(db_env, DocumentJob, values)
    with Session(db_env) as session, session.begin():
        job = session.get(DocumentJob, job_id)
        job.state = "running"
        job.attempts = 1
        job.started_at = NOW
        job.lease_token = uuid4()
        job.lease_expires_at = NOW + timedelta(seconds=60)
        session.flush()
        assert job.lease_token is not None
        job.state = "succeeded"
        job.finished_at = NOW + timedelta(seconds=1)
        job.lease_token = None
        job.lease_expires_at = None
    # Completed extraction history does not prevent a later explicit job.
    with db_env.begin() as connection:
        connection.execute(DocumentJob.__table__.insert().values(**values))
        connection.execute(
            DocumentJob.__table__.insert().values(**(values | {"kind": "delete_files"}))
        )
    reject_values(db_env, DocumentJob, values | {"kind": "delete_files"})


def test_cleanup_address_survives_document_and_file_deletion(db_env, storage_records):
    with db_env.begin() as connection:
        connection.execute(DocumentFile.__table__.insert().values(**file_values(storage_records)))
        connection.execute(
            DocumentSection.__table__.insert().values(
                document_id=storage_records["document"], ordinal=0, text="Extracted text"
            )
        )
        connection.execute(
            DocumentJob.__table__.insert().values(
                **(job_values(storage_records) | {"kind": "delete_files"})
            )
        )
        connection.execute(delete(Document).where(Document.id == storage_records["document"]))
        assert connection.scalar(select(DocumentFile.id)) is None
        assert connection.scalar(select(DocumentSection.id)) is None
        job = connection.execute(select(DocumentJob.document_id, DocumentJob.storage_key)).one()
        assert job.document_id is None
        assert job.storage_key == storage_records["storage_key"]


def test_attachment_preserves_uploader_reference(db_env, storage_records):
    with Session(db_env) as session, session.begin():
        uploader = User(
            email=f"{uuid4().hex}@example.invalid", password_hash="unused", display_name="Uploader"
        )
        session.add(uploader)
        session.flush()
        uploader_id = uploader.id
        session.add(DocumentFile(**(file_values(storage_records) | {"uploaded_by": uploader_id})))
    with db_env.begin() as connection:
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(delete(User).where(User.id == uploader_id))
        assert connection.scalar(select(DocumentFile.uploaded_by)) == uploader_id


def test_processing_status_does_not_change_document_lifecycle(db_env, storage_records):
    with Session(db_env) as session, session.begin():
        document = session.get(Document, storage_records["document"])
        document.status = "archived"
        attachment = DocumentFile(**file_values(storage_records), processing_status="ready")
        session.add(attachment)
        session.flush()
        assert document.status == "archived"
        assert attachment.processing_status == "ready"
