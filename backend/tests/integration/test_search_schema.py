"""Real PostgreSQL search schema, preservation and publication security tests.

Every connection comes from the guarded disposable-database fixtures. No test
uses DATABASE_URL directly, touches private storage, or starts vector services.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.schema import CreateSchema

from agentsphere.config import BACKEND_DIR
from agentsphere.models import Document, DocumentFile, DocumentJob, DocumentSection
from agentsphere.search_contracts import EmbeddingProfileSpec, stable_chunk_id
from agentsphere.search_models import (
    DocumentChunk,
    DocumentIndexVersion,
    DocumentSearchSource,
    EmbeddingProfile,
    VectorJob,
)

pytestmark = pytest.mark.postgres

LEGACY_TABLES = (
    "alembic_version",
    "documents",
    "users",
    "sessions",
    "workspaces",
    "workspace_memberships",
    "auth_rate_limits",
)
STORAGE_TABLES = ("document_files", "document_sections", "document_jobs")
SEARCH_TABLES = (
    "embedding_profiles",
    "document_search_sources",
    "document_index_versions",
    "document_chunks",
    "vector_jobs",
)
SECTION_TEXT = "Hello తెలుగు world.\nSecond line."


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def migration_config(connection):
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture
def migration_connection(pg_engine):
    # A transaction owns this random schema, including all DDL and seed records.
    schema = "search_migration_" + uuid4().hex
    with pg_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            yield connection
        finally:
            transaction.rollback()


def snapshots(connection, tables):
    return {
        table: tuple(
            connection.scalars(
                text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text")
            )
        )
        for table in tables
    }


def column_signatures(connection, tables):
    return {
        table: [
            (column["name"], str(column["type"]), column["nullable"], column["default"])
            for column in inspect(connection).get_columns(table)
        ]
        for table in tables
    }


def constraint_and_index_signatures(connection, tables):
    # Compare actual PostgreSQL definitions; Alembic's autogenerator does not
    # report every check-constraint change. Incoming new-table FKs are additive.
    return {
        table: (
            tuple(
                connection.execute(
                    text(
                        "SELECT conname, contype, pg_get_constraintdef(oid, true), "
                        "convalidated, condeferrable, condeferred FROM pg_constraint "
                        "WHERE conrelid = to_regclass(:table) ORDER BY conname"
                    ),
                    {"table": table},
                )
            ),
            tuple(
                connection.execute(
                    text(
                        "SELECT pg_get_indexdef(indexrelid), indisvalid, indisready "
                        "FROM pg_index WHERE indrelid = to_regclass(:table) "
                        "ORDER BY pg_get_indexdef(indexrelid)"
                    ),
                    {"table": table},
                )
            ),
        )
        for table in tables
    }


def seed_legacy(connection):
    user, viewer, workspace, document, archived = [uuid4() for _ in range(5)]
    now = datetime.now(UTC)
    connection.execute(
        text(
            "INSERT INTO users(id,email,password_hash,display_name) VALUES "
            "(:user,'owner@example.invalid','unused-test-hash','Owner'),"
            "(:viewer,'viewer@example.invalid','unused-test-hash','Viewer')"
        ),
        {"user": user, "viewer": viewer},
    )
    connection.execute(
        text("INSERT INTO workspaces(id,name,owner_user_id) VALUES (:workspace,'Preserved',:user)"),
        {"workspace": workspace, "user": user},
    )
    connection.execute(
        text(
            "INSERT INTO workspace_memberships(workspace_id,user_id,role) "
            "VALUES (:workspace,:viewer,'viewer')"
        ),
        {"workspace": workspace, "viewer": viewer},
    )
    connection.execute(
        text(
            "INSERT INTO documents(id,workspace_id,filename,content_type,size_bytes,status) VALUES "
            "(:document,:workspace,'résumé.txt','text/plain',32,'registered'),"
            "(:archived,:workspace,'archive.md','text/markdown',8,'archived')"
        ),
        {"document": document, "archived": archived, "workspace": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO sessions"
            "(id,token_digest,user_id,csrf_token,created_at,last_seen_at,expires_at) "
            "VALUES (:id,:digest,:user,:csrf,:now,:now,:expires)"
        ),
        {
            "id": uuid4(),
            "digest": "1" * 64,
            "user": user,
            "csrf": "2" * 64,
            "now": now,
            "expires": now + timedelta(hours=1),
        },
    )
    connection.execute(
        text(
            "INSERT INTO auth_rate_limits(scope,identity_digest,window_start,count,expires_at) "
            "VALUES ('login_ip',:digest,1,2,:expires)"
        ),
        {"digest": "3" * 64, "expires": now + timedelta(minutes=1)},
    )
    return {
        "user": user,
        "viewer": viewer,
        "workspace": workspace,
        "document": document,
        "other_document": archived,
    }


def seed_storage(connection, records):
    now = datetime.now(UTC)
    records = records | {
        "file": uuid4(),
        "section": uuid4(),
        "epoch": uuid4(),
        "source_hash": "a" * 64,
        "file_hash": digest(SECTION_TEXT),
    }
    key = f"objects/{records['workspace'].hex}/{uuid4().hex}/original"
    connection.execute(
        DocumentFile.__table__.insert().values(
            id=records["file"],
            document_id=records["document"],
            uploaded_by=records["user"],
            storage_key=key,
            original_filename="original.txt",
            sha256=records["file_hash"],
            processing_status="ready",
            parser_version="test_v1",
            processed_at=now,
        )
    )
    connection.execute(
        DocumentSection.__table__.insert().values(
            id=records["section"],
            document_id=records["document"],
            ordinal=0,
            text=SECTION_TEXT,
            heading="Paragraph 1 · Overview",
            line_start=4,
            line_end=5,
        )
    )
    connection.execute(
        DocumentJob.__table__.insert().values(
            document_id=records["document"],
            storage_key=key,
            kind="extract",
            state="succeeded",
            attempts=1,
            started_at=now - timedelta(seconds=2),
            finished_at=now,
        )
    )
    return records


def profile_values(**changes):
    spec = EmbeddingProfileSpec(model_revision="1" * 40, tokenizer_revision="2" * 40)
    values = spec.model_dump(exclude={"profile_schema_version"}) | changes
    # Hash even intentionally invalid scalar fields so a fingerprint error cannot
    # accidentally mask the constraint a negative profile test intends to check.
    raw_spec = {
        field: values[field]
        for field in EmbeddingProfileSpec.model_fields
        if field != "profile_schema_version"
    }
    raw_spec["profile_schema_version"] = 1
    fingerprint = digest(
        json.dumps(raw_spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    values.setdefault("fingerprint", fingerprint)
    values.setdefault("collection_name", "as_" + fingerprint)
    return values


def source_values(records, **changes):
    return (
        dict(
            document_id=records["document"],
            file_id=records["file"],
            extraction_epoch=records["epoch"],
            source_fingerprint=records["source_hash"],
            file_sha256=records["file_hash"],
            parser_version="test_v1",
        )
        | changes
    )


def seed_search(connection):
    records = seed_storage(connection, seed_legacy(connection))
    records |= {"profile": uuid4(), "generation": uuid4(), "claim_token": uuid4()}
    values = profile_values()
    records |= {"profile_hash": values["fingerprint"], "collection": values["collection_name"]}
    connection.execute(EmbeddingProfile.__table__.insert().values(id=records["profile"], **values))
    connection.execute(DocumentSearchSource.__table__.insert().values(**source_values(records)))
    return records


@pytest.fixture
def search_records(db_env):
    with db_env.begin() as connection:
        yield connection, seed_search(connection)


def generation_values(records, **changes):
    return (
        dict(
            id=records["generation"],
            document_id=records["document"],
            profile_id=records["profile"],
            extraction_epoch=records["epoch"],
            source_fingerprint=records["source_hash"],
            claim_token=records["claim_token"],
        )
        | changes
    )


def add_generation(connection, records, **changes):
    values = generation_values(records) | changes
    connection.execute(DocumentIndexVersion.__table__.insert().values(**values))
    return values["id"]


def job_values(records, **changes):
    return (
        dict(
            document_id=records["document"],
            profile_id=records["profile"],
            generation_id=records["generation"],
            workspace_id=records["workspace"],
            target_document_id=records["document"],
            target_profile_id=records["profile"],
            target_generation_id=records["generation"],
            extraction_epoch=records["epoch"],
            source_fingerprint=records["source_hash"],
            profile_fingerprint=records["profile_hash"],
            collection_name=records["collection"],
            kind="index",
        )
        | changes
    )


def add_job(connection, records, **changes):
    identifier = uuid4()
    connection.execute(
        VectorJob.__table__.insert().values(id=identifier, **job_values(records, **changes))
    )
    return identifier


def claim_job(connection, identifier, records, **changes):
    now = datetime.now(UTC)
    values = (
        dict(
            state="running",
            attempts=1,
            lease_generation=1,
            lease_token=records["claim_token"],
            started_at=now,
            lease_expires_at=now + timedelta(minutes=5),
        )
        | changes
    )
    connection.execute(update(VectorJob).where(VectorJob.id == identifier).values(**values))


def chunk_values(records, **changes):
    return (
        dict(
            generation_id=records["generation"],
            logical_id=stable_chunk_id(
                document_id=records["document"],
                source_fingerprint=records["source_hash"],
                profile_fingerprint=records["profile_hash"],
                section_ordinal=0,
                char_start=0,
                char_end=len(SECTION_TEXT),
                content_sha256=digest(SECTION_TEXT),
            ),
            ordinal=0,
            section_id=records["section"],
            section_ordinal=0,
            text=SECTION_TEXT,
            content_sha256=digest(SECTION_TEXT),
            embedding_input_sha256=digest("passage: " + SECTION_TEXT),
            token_count=12,
            char_start=0,
            char_end=len(SECTION_TEXT),
            heading="Paragraph 1 · Overview",
            line_start=4,
            line_end=5,
        )
        | changes
    )


def add_chunk(connection, records, **changes):
    identifier = uuid4()
    connection.execute(
        DocumentChunk.__table__.insert().values(id=identifier, **chunk_values(records, **changes))
    )
    return identifier


def publish(connection, records, **changes):
    now = datetime.now(UTC)
    values = (
        dict(
            state="ready",
            is_active=True,
            expected_chunk_count=1,
            manifest_sha256="d" * 64,
            verified_at=now,
            completed_at=now,
        )
        | changes
    )
    connection.execute(
        update(DocumentIndexVersion)
        .where(DocumentIndexVersion.id == records["generation"])
        .values(**values)
    )


def build_ready(connection, records):
    add_generation(connection, records)
    job = add_job(connection, records)
    claim_job(connection, job, records)
    chunk = add_chunk(connection, records)
    publish(connection, records)
    return job, chunk


def reject(connection, statement):
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(statement)


def test_clean_upgrade_has_five_empty_tables_and_consistent_metadata(migration_connection):
    connection = migration_connection
    config = migration_config(connection)
    command.upgrade(config, "head")
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
    assert set(SEARCH_TABLES) <= set(inspect(connection).get_table_names())
    assert all(
        connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0 for table in SEARCH_TABLES
    )
    assert (
        connection.scalar(
            text(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname LIKE 'as_search_%' AND NOT tgisinternal "
                "AND tgrelid IN (SELECT c.oid FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema())"
            )
        )
        == 6
    )
    command.check(config)


@pytest.mark.parametrize("starting_revision", ["0002", "0003"])
def test_populated_upgrade_preserves_every_legacy_value_and_schema(
    migration_connection, starting_revision
):
    connection = migration_connection
    config = migration_config(connection)
    command.upgrade(config, starting_revision)
    records = seed_legacy(connection)
    tables = tuple(table for table in LEGACY_TABLES if table != "alembic_version")
    if starting_revision == "0003":
        seed_storage(connection, records)
        tables += STORAGE_TABLES
    before = snapshots(connection, tables)
    columns = column_signatures(connection, tables)
    constraints = constraint_and_index_signatures(connection, tables)
    command.upgrade(config, "0004")
    assert snapshots(connection, tables) == before
    assert column_signatures(connection, tables) == columns
    assert constraint_and_index_signatures(connection, tables) == constraints
    assert all(
        connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0 for table in SEARCH_TABLES
    )
    command.check(config)
    # Empty Module 6 downgrade removes its own guards only and preserves Module 5.
    command.downgrade(config, "0003")
    assert snapshots(connection, tables) == before
    assert column_signatures(connection, tables) == columns
    assert constraint_and_index_signatures(connection, tables) == constraints
    assert set(SEARCH_TABLES).isdisjoint(inspect(connection).get_table_names())
    command.upgrade(config, "0004")
    command.check(config)


@pytest.mark.parametrize("populated_table", SEARCH_TABLES)
def test_downgrade_refuses_populated_search_data_without_partial_loss(
    migration_connection, populated_table
):
    connection = migration_connection
    config = migration_config(connection)
    command.upgrade(config, "0004")
    records = seed_search(connection)
    if populated_table == "embedding_profiles":
        connection.execute(delete(DocumentSearchSource))
    if populated_table == "document_search_sources":
        connection.execute(delete(EmbeddingProfile))
    if populated_table in {"document_index_versions", "document_chunks", "vector_jobs"}:
        add_generation(connection, records)
    if populated_table == "document_chunks":
        add_chunk(connection, records)
    if populated_table == "vector_jobs":
        add_job(connection, records, kind="delete_generation")
        connection.execute(delete(Document).where(Document.id == records["document"]))
        connection.execute(delete(EmbeddingProfile))
        connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert all(
            connection.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            for table in SEARCH_TABLES
            if table != "vector_jobs"
        )
    before = snapshots(connection, LEGACY_TABLES + STORAGE_TABLES + SEARCH_TABLES)
    with pytest.raises(DBAPIError, match="Downgrade refused: Module 6"):
        with connection.begin_nested():
            command.downgrade(config, "0003")
    assert snapshots(connection, LEGACY_TABLES + STORAGE_TABLES + SEARCH_TABLES) == before
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0004"


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "hosted"},
        {"dimensions": 768},
        {"model_name": "unreviewed-model"},
        {"model_revision": "main"},
        {"tokenizer_revision": "A" * 40},
        {"normalize_embeddings": False},
        {"query_prefix": ""},
        {"distance_metric": "dot"},
        {"chunk_tokens": 481},
        {"chunk_tokens": 50},
        {"chunk_overlap_tokens": -1},
        {"fingerprint": "not-a-hash"},
        {"collection_name": "another_collection"},
    ],
)
def test_profile_database_constraints_reject_invalid_specs(search_records, changes):
    connection, _records = search_records
    # Avoid the existing fingerprint so uniqueness cannot mask a missing check.
    values = profile_values(**({"model_revision": "3" * 40} | changes))
    reject(connection, EmbeddingProfile.__table__.insert().values(**values))


def test_profile_fingerprint_is_unique_and_persisted_configuration_immutable(search_records):
    connection, records = search_records
    reject(connection, EmbeddingProfile.__table__.insert().values(**profile_values()))
    for changes in ({"chunk_tokens": 351}, {"fingerprint": "b" * 64}, {"model_revision": "3" * 40}):
        reject(
            connection,
            update(EmbeddingProfile)
            .where(EmbeddingProfile.id == records["profile"])
            .values(**changes),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"file_sha256": "b" * 64},
        {"parser_version": "wrong_parser"},
        {"source_fingerprint": "invalid"},
        {"file_id": uuid4()},
    ],
)
def test_source_requires_matching_validated_attachment(search_records, changes):
    connection, records = search_records
    connection.execute(delete(DocumentSearchSource))
    reject(
        connection,
        DocumentSearchSource.__table__.insert().values(**source_values(records, **changes)),
    )


def test_source_cannot_reference_another_document_or_unvalidated_processing(search_records):
    connection, records = search_records
    connection.execute(delete(DocumentSearchSource))
    reject(
        connection,
        DocumentSearchSource.__table__.insert().values(
            **source_values(records, document_id=records["other_document"])
        ),
    )
    connection.execute(update(DocumentFile).values(processing_status="processing"))
    reject(connection, DocumentSearchSource.__table__.insert().values(**source_values(records)))


def test_source_change_requires_new_epoch(search_records):
    connection, _records = search_records
    reject(connection, update(DocumentSearchSource).values(source_fingerprint="b" * 64))


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "ready"},
        {"is_active": True},
        {"extraction_epoch": uuid4()},
        {"source_fingerprint": "b" * 64},
        {"profile_id": uuid4()},
        {"expected_chunk_count": -1},
        {"expected_chunk_count": 10001},
    ],
)
def test_generations_begin_unpublished_and_use_current_source(search_records, changes):
    connection, records = search_records
    reject(
        connection,
        DocumentIndexVersion.__table__.insert().values(**generation_values(records, **changes)),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"section_id": uuid4()},
        {"section_id": None},
        {"section_ordinal": 1},
        {"heading": "Invented heading"},
        {"page_number": 1},
        {"line_start": 3},
        {"source_precision": "exact"},
        {"content_sha256": "b" * 64},
        {"embedding_input_sha256": "c" * 64},
        {"token_count": 0},
        {"token_count": 351},
        {"ordinal": -1},
        {"char_start": 1},
        {"char_end": len(SECTION_TEXT) + 1},
        {"text": "Invented text"},
    ],
)
def test_chunk_constraints_reject_forged_text_locations_and_inputs(search_records, changes):
    connection, records = search_records
    add_generation(connection, records)
    reject(connection, DocumentChunk.__table__.insert().values(**chunk_values(records, **changes)))


def test_chunk_cannot_use_a_section_from_another_document(search_records):
    connection, records = search_records
    other = uuid4()
    connection.execute(
        DocumentSection.__table__.insert().values(
            id=other,
            document_id=records["other_document"],
            ordinal=0,
            text=SECTION_TEXT,
            heading="Paragraph 1 · Overview",
            line_start=4,
            line_end=5,
        )
    )
    add_generation(connection, records)
    reject(
        connection,
        DocumentChunk.__table__.insert().values(**chunk_values(records, section_id=other)),
    )


def test_generation_chunk_ordinals_and_logical_ids_are_unique(search_records):
    connection, records = search_records
    add_generation(connection, records)
    add_chunk(connection, records)
    reject(
        connection,
        DocumentChunk.__table__.insert().values(**chunk_values(records, logical_id=uuid4())),
    )
    reject(connection, DocumentChunk.__table__.insert().values(**chunk_values(records, ordinal=1)))


@pytest.mark.parametrize("failure", ["no_chunks", "gap", "expired_lease", "wrong_claim"])
def test_generation_publication_rejects_incomplete_or_unfenced_results(search_records, failure):
    connection, records = search_records
    add_generation(connection, records)
    job = add_job(connection, records)
    claim_job(connection, job, records)
    if failure != "no_chunks":
        add_chunk(connection, records, ordinal=1 if failure == "gap" else 0)
    if failure == "expired_lease":
        now = datetime.now(UTC)
        connection.execute(
            update(VectorJob)
            .where(VectorJob.id == job)
            .values(
                started_at=now - timedelta(minutes=2), lease_expires_at=now - timedelta(minutes=1)
            )
        )
    if failure == "wrong_claim":
        connection.execute(
            update(VectorJob)
            .where(VectorJob.id == job)
            .values(state="queued", lease_token=None, lease_expires_at=None)
        )
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            publish(connection, records)
    assert connection.scalar(select(DocumentIndexVersion.state)) == "building"
    assert connection.scalar(select(DocumentIndexVersion.is_active)) is False


def test_successful_publication_is_readable_only_as_one_active_generation(search_records):
    connection, records = search_records
    job, chunk = build_ready(connection, records)
    assert connection.scalar(select(DocumentIndexVersion.is_active)) is True
    assert connection.scalar(select(DocumentChunk.text)) == SECTION_TEXT
    reject(
        connection, update(DocumentChunk).where(DocumentChunk.id == chunk).values(token_count=13)
    )
    reject(connection, update(DocumentIndexVersion).values(expected_chunk_count=2))
    reject(
        connection,
        update(DocumentIndexVersion).values(state="building", completed_at=None, verified_at=None),
    )
    connection.execute(
        update(VectorJob)
        .where(VectorJob.id == job)
        .values(
            state="succeeded",
            finished_at=datetime.now(UTC),
            lease_token=None,
            lease_expires_at=None,
        )
    )
    second = records | {"generation": uuid4(), "claim_token": uuid4()}
    add_generation(connection, second)
    second_job = add_job(connection, second)
    claim_job(connection, second_job, second)
    add_chunk(connection, second)
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            publish(connection, second)
    assert (
        connection.scalar(select(DocumentIndexVersion.id).where(DocumentIndexVersion.is_active))
        == records["generation"]
    )


@pytest.mark.parametrize("changed_source", ["text", "heading", "page", "lines", "file_document"])
def test_publication_revalidates_live_content_and_source_association(
    search_records, changed_source
):
    connection, records = search_records
    add_generation(connection, records)
    job = add_job(connection, records)
    claim_job(connection, job, records)
    add_chunk(connection, records)
    if changed_source == "file_document":
        connection.execute(
            update(DocumentFile)
            .where(DocumentFile.id == records["file"])
            .values(document_id=records["other_document"])
        )
    else:
        values = {
            "text": {"text": "X" + SECTION_TEXT[1:]},
            "heading": {"heading": "Changed heading"},
            "page": {"page_number": 3},
            "lines": {"line_start": 10, "line_end": 11},
        }[changed_source]
        connection.execute(
            update(DocumentSection).where(DocumentSection.id == records["section"]).values(**values)
        )
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            publish(connection, records)
    assert connection.scalar(select(DocumentIndexVersion.is_active)) is False


def test_empty_generation_is_explicit_and_cannot_be_active(search_records):
    connection, records = search_records
    add_generation(connection, records)
    job = add_job(connection, records)
    claim_job(connection, job, records)
    publish(connection, records, state="empty", is_active=False, expected_chunk_count=0)
    assert connection.scalar(select(DocumentIndexVersion.state)) == "empty"
    reject(connection, update(DocumentIndexVersion).values(is_active=True))


def test_reused_lease_token_cannot_publish_previous_attempt_generation(search_records):
    connection, records = search_records
    add_generation(connection, records)
    job = add_job(connection, records)
    claim_job(connection, job, records)
    add_chunk(connection, records)
    connection.execute(
        update(VectorJob)
        .where(VectorJob.id == job)
        .values(state="queued", lease_token=None, lease_expires_at=None)
    )
    # Deliberately reuse the UUID to prove the monotonic fence, rather than UUID
    # randomness alone, keeps the prior generation from publishing.
    claim_job(connection, job, records, attempts=2, lease_generation=2)
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            publish(connection, records)
    assert connection.scalar(select(DocumentIndexVersion.is_active)) is False


@pytest.mark.parametrize("locked_row", ["source", "job"])
def test_publication_fails_fast_when_source_or_lease_is_locked(db_env, locked_row):
    # Commit setup so independent connections see the same records and exercise
    # real PostgreSQL row locks rather than an in-memory concurrency substitute.
    with db_env.begin() as setup:
        records = seed_search(setup)
        add_generation(setup, records)
        job = add_job(setup, records)
        claim_job(setup, job, records)
        add_chunk(setup, records)
    with db_env.begin() as locker:
        if locked_row == "source":
            statement = (
                select(DocumentSearchSource)
                .where(DocumentSearchSource.document_id == records["document"])
                .with_for_update()
            )
        else:
            statement = select(VectorJob).where(VectorJob.id == job).with_for_update()
        locker.execute(statement).one()
        with pytest.raises(DBAPIError) as failure:
            with db_env.begin() as publisher:
                # A regressed blocking lock fails with query_canceled instead of
                # hanging indefinitely. NOWAIT must produce lock_not_available.
                publisher.execute(text("SET LOCAL statement_timeout = '2000ms'"))
                publish(publisher, records)
        assert failure.value.orig.sqlstate == "55P03"
    with db_env.connect() as connection:
        assert connection.scalar(select(DocumentIndexVersion.state)) == "building"
        assert connection.scalar(select(DocumentIndexVersion.is_active)) is False


@pytest.mark.parametrize("publication_started", [False, True])
def test_chunk_delete_cannot_race_an_uncommitted_publication(db_env, publication_started):
    with db_env.begin() as setup:
        records = seed_search(setup)
        add_generation(setup, records)
        job = add_job(setup, records)
        claim_job(setup, job, records)
        chunk = add_chunk(setup, records)
    with db_env.begin() as publisher:
        publisher.execute(
            select(DocumentIndexVersion.id)
            .where(DocumentIndexVersion.id == records["generation"])
            .with_for_update()
        ).one()
        if publication_started:
            publish(publisher, records)
        # Another transaction still sees the committed 'building' state. It
        # must lock the generation before deciding whether deletion is safe.
        with pytest.raises(DBAPIError) as failure:
            with db_env.begin() as deleter:
                deleter.execute(text("SET LOCAL statement_timeout = '2000ms'"))
                deleter.execute(delete(DocumentChunk).where(DocumentChunk.id == chunk))
        assert failure.value.orig.sqlstate == "55P03"
        if not publication_started:
            publish(publisher, records)
    with db_env.connect() as connection:
        assert connection.scalar(select(DocumentIndexVersion.is_active)) is True
        assert connection.scalar(select(DocumentChunk.id)) == chunk


def test_chunk_delete_locks_building_generation_until_it_commits(db_env):
    with db_env.begin() as setup:
        records = seed_search(setup)
        add_generation(setup, records)
        job = add_job(setup, records)
        claim_job(setup, job, records)
        chunk = add_chunk(setup, records)
    with db_env.begin() as deleter:
        deleter.execute(delete(DocumentChunk).where(DocumentChunk.id == chunk))
        with pytest.raises(DBAPIError) as failure:
            with db_env.begin() as publisher:
                publisher.execute(text("SET LOCAL lock_timeout = '100ms'"))
                publish(publisher, records)
        assert failure.value.orig.sqlstate == "55P03"
    # After the deletion commits, publication must reject the incomplete set.
    with pytest.raises(IntegrityError):
        with db_env.begin() as publisher:
            publish(publisher, records)
    with db_env.connect() as connection:
        assert connection.scalar(select(DocumentIndexVersion.is_active)) is False
        assert connection.scalar(select(DocumentChunk.id)) is None


def test_old_repeatable_read_snapshot_cannot_publish_deleted_chunks(db_env):
    with db_env.begin() as setup:
        records = seed_search(setup)
        add_generation(setup, records)
        job = add_job(setup, records)
        claim_job(setup, job, records)
        add_chunk(setup, records)
    with db_env.connect().execution_options(isolation_level="REPEATABLE READ") as publisher:
        with publisher.begin():
            assert publisher.scalar(select(DocumentChunk.id)) is not None
            with db_env.begin() as deleter:
                deleter.execute(delete(DocumentChunk))
            # A retained MVCC snapshot is not authoritative for completeness.
            with pytest.raises(IntegrityError, match="READ COMMITTED"):
                with publisher.begin_nested():
                    publish(publisher, records)
    with db_env.connect() as connection:
        assert connection.scalar(select(DocumentIndexVersion.is_active)) is False
        assert connection.scalar(select(DocumentChunk.id)) is None


def test_new_extraction_epoch_obsoletes_old_results_and_cancels_old_jobs(search_records):
    connection, records = search_records
    job, _chunk = build_ready(connection, records)
    connection.execute(update(DocumentSearchSource).values(extraction_epoch=uuid4()))
    assert connection.scalar(select(DocumentIndexVersion.state)) == "obsolete"
    assert connection.scalar(select(DocumentIndexVersion.is_active)) is False
    assert connection.scalar(select(VectorJob.state).where(VectorJob.id == job)) == "cancelled"
    reject(connection, update(DocumentSearchSource).values(extraction_epoch=records["epoch"]))
    reject(connection, update(DocumentIndexVersion).values(state="ready", is_active=True))


def test_legacy_section_replacement_invalidates_generation_without_blocking_extraction(
    search_records,
):
    connection, records = search_records
    build_ready(connection, records)
    connection.execute(delete(DocumentSection).where(DocumentSection.id == records["section"]))
    connection.execute(
        DocumentSection.__table__.insert().values(
            document_id=records["document"], ordinal=0, text=SECTION_TEXT
        )
    )
    assert connection.scalar(select(DocumentChunk.section_id)) is None
    assert connection.scalar(select(DocumentIndexVersion.state)) == "obsolete"
    assert connection.scalar(select(DocumentIndexVersion.is_active)) is False


def test_cleanup_address_and_tombstone_survive_source_and_profile_deletion(search_records):
    connection, records = search_records
    build_ready(connection, records)
    cleanup = add_job(connection, records, kind="delete_generation")
    snapshot_fields = (
        VectorJob.workspace_id,
        VectorJob.target_document_id,
        VectorJob.target_profile_id,
        VectorJob.target_generation_id,
        VectorJob.collection_name,
        VectorJob.profile_fingerprint,
        VectorJob.extraction_epoch,
        VectorJob.source_fingerprint,
    )
    before = connection.execute(select(*snapshot_fields).where(VectorJob.id == cleanup)).one()
    connection.execute(delete(Document).where(Document.id == records["document"]))
    connection.execute(delete(EmbeddingProfile).where(EmbeddingProfile.id == records["profile"]))
    connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert (
        connection.execute(select(*snapshot_fields).where(VectorJob.id == cleanup)).one() == before
    )
    assert connection.execute(
        select(
            VectorJob.document_id, VectorJob.profile_id, VectorJob.generation_id, VectorJob.state
        ).where(VectorJob.id == cleanup)
    ).one() == (None, None, None, "queued")
    assert (
        connection.scalar(select(VectorJob.state).where(VectorJob.kind == "index")) == "cancelled"
    )
    assert connection.scalar(select(DocumentIndexVersion.id)) is None
    assert connection.scalar(select(DocumentChunk.id)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"workspace_id": uuid4()},
        {"target_document_id": uuid4()},
        {"target_profile_id": uuid4()},
        {"profile_fingerprint": "b" * 64},
        {"collection_name": "public"},
        {"source_fingerprint": "b" * 64},
        {"extraction_epoch": uuid4()},
        {"state": "running"},
        {"attempts": 1},
        {"lease_generation": 1},
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"error_code": "private/path detail"},
    ],
)
def test_job_creation_rejects_forged_scope_or_invalid_state(search_records, changes):
    connection, records = search_records
    add_generation(connection, records)
    reject(connection, VectorJob.__table__.insert().values(**job_values(records, **changes)))


def test_one_active_index_request_and_permanent_cleanup_tombstone(search_records):
    connection, records = search_records
    add_generation(connection, records)
    add_job(connection, records)
    reject(connection, VectorJob.__table__.insert().values(**job_values(records)))
    cleanup = add_job(connection, records, kind="delete_generation")
    claim_job(connection, cleanup, records)
    connection.execute(
        update(VectorJob)
        .where(VectorJob.id == cleanup)
        .values(
            state="succeeded",
            finished_at=datetime.now(UTC),
            lease_token=None,
            lease_expires_at=None,
        )
    )
    reject(
        connection,
        VectorJob.__table__.insert().values(**job_values(records, kind="delete_generation")),
    )


def test_job_claim_and_retry_require_increasing_fences_and_bounded_attempts(search_records):
    connection, records = search_records
    add_generation(connection, records)
    job = add_job(connection, records, max_attempts=1)
    now = datetime.now(UTC)
    reject(
        connection,
        update(VectorJob)
        .where(VectorJob.id == job)
        .values(
            state="running",
            attempts=1,
            lease_generation=0,
            lease_token=records["claim_token"],
            started_at=now,
            lease_expires_at=now + timedelta(minutes=1),
        ),
    )
    claim_job(connection, job, records)
    reject(connection, update(VectorJob).where(VectorJob.id == job).values(lease_token=uuid4()))
    connection.execute(
        update(VectorJob)
        .where(VectorJob.id == job)
        .values(state="queued", lease_token=None, lease_expires_at=None)
    )
    reject(
        connection,
        update(VectorJob)
        .where(VectorJob.id == job)
        .values(
            state="running",
            attempts=2,
            lease_generation=2,
            lease_token=records["claim_token"],
            started_at=now,
            lease_expires_at=now + timedelta(minutes=1),
        ),
    )


def test_terminal_jobs_and_frozen_cleanup_scope_cannot_be_reused(search_records):
    connection, records = search_records
    add_generation(connection, records)
    cleanup = add_job(connection, records, kind="delete_generation")
    reject(
        connection,
        update(VectorJob).where(VectorJob.id == cleanup).values(target_generation_id=uuid4()),
    )
    claim_job(connection, cleanup, records)
    connection.execute(
        update(VectorJob)
        .where(VectorJob.id == cleanup)
        .values(
            state="failed",
            finished_at=datetime.now(UTC),
            error_code="storage_unavailable",
            lease_token=None,
            lease_expires_at=None,
        )
    )
    reject(
        connection,
        update(VectorJob).where(VectorJob.id == cleanup).values(state="queued", finished_at=None),
    )


def test_linked_index_job_cannot_clear_its_retained_generation_target(search_records):
    # SQL CHECK expressions treat NULL as satisfied unless it is rejected explicitly.
    connection, records = search_records
    add_generation(connection, records)
    job = add_job(connection, records)
    reject(
        connection,
        update(VectorJob).where(VectorJob.id == job).values(target_generation_id=None),
    )
    assert (
        connection.scalar(select(VectorJob.target_generation_id).where(VectorJob.id == job))
        == records["generation"]
    )
