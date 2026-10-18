"""Real isolated PostgreSQL checks for unavailable originals and failed finalization."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from threading import Event, current_thread
from time import monotonic
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic import command
from conftest import migration_config, register_and_login
from fastapi import HTTPException
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_document_uploads import private_files, send_file, trust_extraction, uploaded

from agentsphere.api import document_files
from agentsphere.models import Document, DocumentFile, DocumentJob, JobKind, JobState, Workspace
from agentsphere.storage import PrivateStorage, StorageError, new_storage_key

pytestmark = pytest.mark.postgres


@pytest.fixture
def private_client(client_factory, tmp_path):
    return register_and_login(client_factory(private_storage_root=tmp_path / "private"))


@pytest.mark.parametrize("damage", ["missing", "corrupt", "oversized"])
def test_validated_but_unavailable_original_returns_sanitized_failure(
    private_client, db_env, clock, damage
):
    body = uploaded(private_client)
    trust_extraction(db_env, body["document"]["id"], clock)
    original = private_files(private_client)[0]
    if damage == "missing":
        original.unlink()
    elif damage == "corrupt":
        original.write_bytes(b"X" * original.stat().st_size)
    else:
        original.write_bytes(b"X" * (original.stat().st_size + 1))
    url = f"{private_client.documents_url}/{body['document']['id']}/download"
    response = private_client.get(url)
    assert response.status_code == 503, response.text
    assert response.json() == {"detail": "Private file is unavailable."}
    assert str(original) not in response.text
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"
    # An unavailable physical object must not silently discard its valid metadata.
    assert private_client.get(url.removesuffix("/download")).status_code == 200


def test_compensation_never_deletes_an_already_committed_original(
    private_client, db_env, monkeypatch
):
    body = uploaded(private_client)
    with Session(db_env) as session:
        key = session.get(DocumentFile, UUID(body["file"]["id"])).storage_key
    original = private_files(private_client)[0]
    expected_bytes = original.read_bytes()

    def forbidden_delete(*args):
        pytest.fail("Compensation attempted to delete a committed original")

    monkeypatch.setattr(PrivateStorage, "delete", forbidden_delete)
    document_files.compensate(
        SimpleNamespace(app=private_client.app),
        private_client.app.state.private_storage,
        key,
        UUID(private_client.workspace_id),
    )
    assert original.read_bytes() == expected_bytes
    with Session(db_env) as session:
        assert session.get(DocumentFile, UUID(body["file"]["id"])) is not None
        assert session.scalar(select(func.count()).select_from(DocumentJob)) == 1


@contextmanager
def unresolved_upload(private_client, db_env):
    """An original is published while finalization holds uncommitted rows/locks."""
    request = SimpleNamespace(app=private_client.app)
    storage = document_files.get_storage(request)
    file_id = uuid4()
    workspace_id = UUID(private_client.workspace_id)
    key = new_storage_key(workspace_id, file_id)
    payload = b"An uncertain commit must not lose its private original.\n"
    stored = storage.put(key, BytesIO(payload), max_bytes=10_000_000)
    original = private_client.app.state.settings.private_storage_root.joinpath(*key.split("/"))
    with Session(db_env) as pending:
        assert pending.get(Workspace, workspace_id, with_for_update=True) is not None
        document = Document(
            workspace_id=workspace_id,
            filename="uncertain.txt",
            content_type="text/plain",
            size_bytes=stored.size_bytes,
        )
        pending.add(document)
        pending.flush()
        pending.add(
            DocumentFile(
                id=file_id,
                document_id=document.id,
                storage_key=key,
                original_filename=document.filename,
                sha256=stored.sha256,
                uploaded_by=UUID(private_client.user_id),
            )
        )
        pending.add(DocumentJob(document_id=document.id, storage_key=key, kind=JobKind.EXTRACT))
        pending.flush()
        yield pending, request, storage, key, original, payload, document.id


@pytest.mark.parametrize("outcome", ["commit", "rollback"])
def test_compensation_waits_for_uncertain_commit_before_deciding_file_ownership(
    private_client, db_env, outcome
):
    started = Event()
    poll_pause = Event()
    backend_pid = []

    def observe_compensation_query(connection, cursor, statement, parameters, context, executemany):
        if current_thread().name.startswith(
            "upload-compensation"
        ) and statement.lstrip().lower().startswith("select"):
            backend_pid[:] = [connection.connection.driver_connection.info.backend_pid]
            started.set()

    with unresolved_upload(private_client, db_env) as pending_upload:
        pending, request, storage, key, original, payload, document_id = pending_upload
        event.listen(db_env, "before_cursor_execute", observe_compensation_query)
        try:
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="upload-compensation"
            ) as pool:
                future = pool.submit(
                    document_files.compensate,
                    request,
                    storage,
                    key,
                    UUID(private_client.workspace_id),
                )
                try:
                    assert started.wait(3), "Compensation did not issue its database lookup."
                    deadline = monotonic() + 1.5
                    with db_env.connect() as observer:
                        while monotonic() < deadline:
                            assert not future.done(), "Compensation did not wait for finalization."
                            if observer.scalar(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_locks "
                                    "WHERE pid = :pid AND NOT granted)"
                                ),
                                {"pid": backend_pid[0]},
                            ):
                                break
                            poll_pause.wait(0.01)
                        else:
                            pytest.fail(
                                "Compensation did not wait on the unresolved workspace lock."
                            )
                    assert not future.done()
                    assert original.read_bytes() == payload
                    if outcome == "commit":
                        pending.commit()
                    else:
                        pending.rollback()
                    future.result(timeout=3)
                finally:
                    pending.rollback()
                    future.result(timeout=5)
        finally:
            event.remove(db_env, "before_cursor_execute", observe_compensation_query)
        with Session(db_env) as session:
            attachment = session.scalar(
                select(DocumentFile).where(DocumentFile.document_id == document_id)
            )
            if outcome == "commit":
                assert attachment is not None
                assert original.read_bytes() == payload
            else:
                assert attachment is None
                assert not original.exists()


def test_compensation_lock_timeout_retains_original_until_commit_is_resolved(
    private_client, db_env, caplog
):
    with unresolved_upload(private_client, db_env) as pending_upload:
        pending, request, storage, key, original, payload, document_id = pending_upload
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                document_files.compensate,
                request,
                storage,
                key,
                UUID(private_client.workspace_id),
            )
            future.result(timeout=5)
        assert "Upload compensation deferred" in caplog.text
        assert "OperationalError" in caplog.text or "HTTPException" in caplog.text
        assert str(original) not in caplog.text
        assert "lock timeout" not in caplog.text
        assert original.read_bytes() == payload
        pending.commit()
    with Session(db_env) as session:
        assert (
            session.scalar(select(DocumentFile.id).where(DocumentFile.document_id == document_id))
            is not None
        )
    assert original.read_bytes() == payload


def test_database_flush_and_delete_failures_leave_a_durable_cleanup_job(
    private_client, db_env, monkeypatch
):
    def fail_attachment_flush(session, context, instances):
        if any(isinstance(item, DocumentFile) for item in session.new):
            raise OperationalError("redacted", {}, Exception("injected private DB detail"))

    def fail_delete(*args):
        raise StorageError("injected private storage detail")

    event.listen(Session, "before_flush", fail_attachment_flush)
    monkeypatch.setattr(PrivateStorage, "delete", fail_delete)
    try:
        response = send_file(private_client)
    finally:
        event.remove(Session, "before_flush", fail_attachment_flush)
    assert response.status_code == 503, response.text
    assert "injected private" not in response.text
    remaining = private_files(private_client)
    assert len(remaining) == 1
    with Session(db_env) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert session.scalar(select(func.count()).select_from(DocumentFile)) == 0
        cleanup = session.scalars(select(DocumentJob)).one()
        assert cleanup.kind == JobKind.DELETE_FILES
        assert cleanup.state == JobState.QUEUED
        assert cleanup.document_id is None and cleanup.attempts == 0
        root = private_client.app.state.settings.private_storage_root
        assert root.joinpath(*cleanup.storage_key.split("/")) == remaining[0]


def test_ambiguous_database_outage_retains_private_orphan_for_reconciliation(
    private_client, db_env, monkeypatch, caplog
):
    real_factory = private_client.app.state.session_factory

    def unreachable_factory():
        raise OperationalError("redacted", {}, Exception("injected DB connection secret"))

    def unavailable_finalization(*args):
        # Auth/preflight already used the real database and storage.put has
        # published bytes. The reconnect needed to resolve outcome now fails.
        private_client.app.state.session_factory = unreachable_factory
        raise HTTPException(503, "Database operation unavailable. Please try again later.")

    monkeypatch.setattr(document_files, "finalize_upload", unavailable_finalization)
    try:
        response = send_file(private_client)
    finally:
        private_client.app.state.session_factory = real_factory
    assert response.status_code == 503, response.text
    assert "injected DB connection secret" not in response.text
    assert "Upload compensation deferred" in caplog.text
    assert "injected DB connection secret" not in caplog.text
    # Deleting on an unknown commit outcome could corrupt an accepted upload.
    assert len(private_files(private_client)) == 1
    with Session(db_env) as session:
        for model in (Document, DocumentFile, DocumentJob):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_deleted_upload_nonce_cannot_recreate_the_document(private_client, db_env):
    headers = {"Idempotency-Key": str(uuid4())}
    body = uploaded(private_client, headers=headers)
    url = f"{private_client.documents_url}/{body['document']['id']}"
    assert private_client.delete(url).status_code == 204
    response = send_file(private_client, headers=headers)
    assert response.status_code == 409, response.text
    assert "new idempotency key" in response.json()["detail"]
    assert private_client.get(url).status_code == 404
    assert len(private_files(private_client)) == 1
    with Session(db_env) as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        cleanup = session.scalar(
            select(DocumentJob).where(DocumentJob.kind == JobKind.DELETE_FILES)
        )
        assert cleanup.state == JobState.QUEUED and cleanup.document_id is None


def test_busy_upload_capacity_is_bounded_without_storage_side_effects(private_client, db_env):
    slots = private_client.app.state.upload_slots
    capacity = private_client.app.state.settings.upload_concurrency

    async def occupy_slots():
        for _ in range(capacity):
            await slots.acquire()

    private_client.portal.call(occupy_slots)
    try:
        response = send_file(private_client)
    finally:
        for _ in range(capacity):
            private_client.portal.call(slots.release)
    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "1"
    assert "capacity is busy" in response.json()["detail"]
    assert private_client.app.state.private_storage is None
    assert private_files(private_client) == []
    with Session(db_env) as session:
        for model in (Document, DocumentFile, DocumentJob):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_revision_0002_keeps_metadata_crud_and_refuses_upload_without_migration(
    db_env, client_factory, tmp_path, metadata
):
    # db_env creates a fresh, random schema inside the disposable verifier DB.
    # Downgrade it while empty, before creating users, sessions or documents.
    with db_env.begin() as connection:
        command.downgrade(migration_config(connection), "0002")
    client = register_and_login(client_factory(private_storage_root=tmp_path / "unmigrated"))
    created = client.post(client.documents_url, json=metadata)
    assert created.status_code == 201, created.text
    url = f"{client.documents_url}/{created.json()['id']}"
    assert client.get(client.documents_url).json()["items"] == [created.json()]
    assert client.get(url).json() == created.json()
    assert client.patch(url, json={"size_bytes": 33, "status": "archived"}).status_code == 200
    assert client.patch(url, json={"status": "registered"}).status_code == 200
    response = send_file(client)
    assert response.status_code == 503, response.text
    assert "0003 migration" in response.json()["detail"]
    assert client.app.state.private_storage is None
    assert private_files(client) == []
    assert client.get(url + "/file").status_code == 503
    assert client.get(url + "/download").status_code == 503
    assert client.delete(url).status_code == 204
    assert client.get(url).status_code == 404
    with db_env.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name IN "
                    "('document_files', 'document_sections', 'document_jobs')"
                )
            )
            == 0
        )
